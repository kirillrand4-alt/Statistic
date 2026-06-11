"""SQLAlchemy ORM models.

The metric tables store **daily-granularity snapshots** — the smallest unit the
upstream APIs expose. Any reporting period is answered by a
``WHERE date BETWEEN ...`` scan plus aggregation, which keeps period-over-period
comparison (feature 5) and arbitrary-range export (feature 4) simple. Unique
constraints on the natural keys make ingestion idempotent (upsert).
"""
from __future__ import annotations

from datetime import date as date_type
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Source(Base):
    """A data source: gsc | yandex_webmaster | yandex_metrika."""

    __tablename__ = "source"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))

    sites: Mapped[list["Site"]] = relationship(back_populates="source")


class Site(Base):
    """A property/host within a source (e.g. ``sc-domain:example.com``)."""

    __tablename__ = "site"
    __table_args__ = (
        UniqueConstraint("source_id", "property_uri", name="uq_site_source_uri"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"), index=True)
    property_uri: Mapped[str] = mapped_column(String(512))
    external_host_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    source: Mapped[Source] = relationship(back_populates="sites")
    projects: Mapped[list["Project"]] = relationship(back_populates="site")


class Project(Base):
    """A saved list of URLs + its scope (feature 3 "subset of pages")."""

    __tablename__ = "project"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    site: Mapped[Site] = relationship(back_populates="projects")
    urls: Mapped[list["ProjectUrl"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectUrl(Base):
    __tablename__ = "project_url"
    __table_args__ = (
        Index("ix_project_url_project", "project_id"),
        Index("ix_project_url_normalized", "normalized_url"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"))
    url: Mapped[str] = mapped_column(String(2048))
    normalized_url: Mapped[str] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    project: Mapped[Project] = relationship(back_populates="urls")


class Page(Base):
    """A unique page within a site; dedupes URLs across daily metric rows."""

    __tablename__ = "page"
    __table_args__ = (
        UniqueConstraint("site_id", "normalized_url", name="uq_page_site_url"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    normalized_url: Mapped[str] = mapped_column(String(2048))
    first_seen: Mapped[date_type | None] = mapped_column(Date, nullable=True)
    last_seen: Mapped[date_type | None] = mapped_column(Date, nullable=True)


class Query(Base):
    """A unique search query within a site."""

    __tablename__ = "query"
    __table_args__ = (
        UniqueConstraint("site_id", "text_hash", name="uq_query_site_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"), index=True)
    text: Mapped[str] = mapped_column(String(2048))
    text_hash: Mapped[str] = mapped_column(String(64))


class PageMetricDaily(Base):
    """Per-page daily metrics (features 2 & 3)."""

    __tablename__ = "page_metric_daily"
    __table_args__ = (
        UniqueConstraint("site_id", "page_id", "date", name="uq_pmd"),
        Index("ix_pmd_site_date", "site_id", "date"),
        Index("ix_pmd_page_date", "page_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    page_id: Mapped[int] = mapped_column(ForeignKey("page.id"))
    date: Mapped[date_type] = mapped_column(Date)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    ctr: Mapped[float] = mapped_column(Float, default=0.0)
    position: Mapped[float] = mapped_column(Float, default=0.0)


class QueryMetricDaily(Base):
    """Per-page-per-query daily metrics (feature 1 + detailed export).

    ``page_id`` is nullable for site-wide query rows; ingestion of per-URL
    queries (Phase 1) always sets it, so the unique constraint reliably drives
    upserts.
    """

    __tablename__ = "query_metric_daily"
    __table_args__ = (
        UniqueConstraint("site_id", "page_id", "query_id", "date", name="uq_qmd"),
        Index("ix_qmd_page_date", "page_id", "date"),
        Index("ix_qmd_site_date_clicks", "site_id", "date", "clicks"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    page_id: Mapped[int | None] = mapped_column(ForeignKey("page.id"), nullable=True)
    query_id: Mapped[int] = mapped_column(ForeignKey("query.id"))
    date: Mapped[date_type] = mapped_column(Date)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    ctr: Mapped[float] = mapped_column(Float, default=0.0)
    position: Mapped[float] = mapped_column(Float, default=0.0)


class SiteTotalDaily(Base):
    """Whole-site daily totals (feature 3).

    Kept separately from the sum of tracked pages because a site's total
    includes untracked URLs and anonymized queries.
    """

    __tablename__ = "site_total_daily"
    __table_args__ = (UniqueConstraint("site_id", "date", name="uq_std"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"), index=True)
    date: Mapped[date_type] = mapped_column(Date)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    ctr: Mapped[float] = mapped_column(Float, default=0.0)
    position: Mapped[float] = mapped_column(Float, default=0.0)


class DeviceMetricDaily(Base):
    """Per-page daily metrics split by device — for fraud (bot) detection."""

    __tablename__ = "device_metric_daily"
    __table_args__ = (
        UniqueConstraint("site_id", "page_id", "date", "device", name="uq_dmd"),
        Index("ix_dmd_site_date", "site_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    page_id: Mapped[int] = mapped_column(ForeignKey("page.id"))
    date: Mapped[date_type] = mapped_column(Date)
    device: Mapped[str] = mapped_column(String(16))  # desktop | mobile | tablet
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    ctr: Mapped[float] = mapped_column(Float, default=0.0)
    position: Mapped[float] = mapped_column(Float, default=0.0)


class IndexedUrlSnapshot(Base):
    """A dated snapshot of URLs in the search index (for in/out comparison)."""

    __tablename__ = "indexed_url_snapshot"
    __table_args__ = (
        UniqueConstraint("site_id", "captured_on", "normalized_url", name="uq_ius"),
        Index("ix_ius_site_date", "site_id", "captured_on"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    captured_on: Mapped[date_type] = mapped_column(Date)
    url: Mapped[str] = mapped_column(String(2048))
    normalized_url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)


class Visit(Base):
    """A Yandex Metrica visit (from the Logs API / uploaded TSV) for analysis."""

    __tablename__ = "visit"
    __table_args__ = (
        UniqueConstraint("site_id", "visit_id", name="uq_visit"),
        Index("ix_visit_site_date", "site_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"), index=True)
    visit_id: Mapped[int] = mapped_column(BigInteger)
    counter_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    date: Mapped[date_type | None] = mapped_column(Date, nullable=True)
    date_time: Mapped[str | None] = mapped_column(String(32), nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    traffic_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    search_engine: Mapped[str | None] = mapped_column(String(64), nullable=True)
    adv_engine: Mapped[str | None] = mapped_column(String(64), nullable=True)
    referer: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    end_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_views: Mapped[int] = mapped_column(Integer, default=0)
    duration: Mapped[int] = mapped_column(Integer, default=0)
    bounce: Mapped[int] = mapped_column(Integer, default=0)
    device: Mapped[str | None] = mapped_column(String(32), nullable=True)
    os: Mapped[str | None] = mapped_column(String(64), nullable=True)
    browser: Mapped[str | None] = mapped_column(String(64), nullable=True)
    region_city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    watch_ids: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppSetting(Base):
    """Runtime-editable key/value config (optionally encrypted)."""

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class CollectionRun(Base):
    """Audit record per (site, job_type, target_date) — drives incremental sync."""

    __tablename__ = "collection_run"
    __table_args__ = (
        Index("ix_cr_site_job_date", "site_id", "job_type", "target_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    job_type: Mapped[str] = mapped_column(String(32))  # page_daily|query_daily|totals|backfill
    target_date: Mapped[date_type | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|ok|error
    rows_written: Mapped[int] = mapped_column(Integer, default=0)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
