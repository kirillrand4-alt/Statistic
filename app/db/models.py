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
    # comma-separated Metrica goal ids to track on this project (empty = all)
    favorite_goals: Mapped[str | None] = mapped_column(Text, nullable=True)

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


class UrlBrand(Base):
    """Per-domain URL -> brand map (uploaded CSV), to filter a project by brand.

    ``url_key`` is host+path (scheme/www/query-agnostic), the same key the goals
    matcher uses, so it lines up with project URLs regardless of UTM tags."""

    __tablename__ = "url_brand"
    __table_args__ = (
        UniqueConstraint("domain", "url_key", "brand", name="uq_url_brand"),
        Index("ix_url_brand_dom_brand", "domain", "brand"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    url_key: Mapped[str] = mapped_column(String(512))
    brand: Mapped[str] = mapped_column(String(128))
    url: Mapped[str | None] = mapped_column(Text, nullable=True)


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
    visit_id: Mapped[str] = mapped_column(String(32))  # Metrica id, kept as text
                                                        # (some exceed 64-bit int)
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
    # All other Logs API fields that don't have a dedicated column, as a JSON object.
    extra: Mapped[str | None] = mapped_column(Text, nullable=True)


class Hit(Base):
    """A Yandex Metrica hit / pageview (Logs API ``source=hits``).

    One row per hit (``ym:pv:watchID``). Far more numerous than visits, so this
    table is opt-in (populated only when hits are downloaded). Frequently queried
    columns are typed; everything else lands in ``extra`` (JSON).
    """

    __tablename__ = "hit"
    __table_args__ = (
        UniqueConstraint("site_id", "watch_id", name="uq_hit"),
        Index("ix_hit_site_date", "site_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"), index=True)
    watch_id: Mapped[str] = mapped_column(String(32))  # Metrica id, kept as text
                                                        # (some exceed 64-bit int)
    counter_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    date: Mapped[date_type | None] = mapped_column(Date, nullable=True)
    date_time: Mapped[str | None] = mapped_column(String(32), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    referer: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    traffic_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    search_engine: Mapped[str | None] = mapped_column(String(64), nullable=True)
    adv_engine: Mapped[str | None] = mapped_column(String(64), nullable=True)
    social_network: Mapped[str | None] = mapped_column(String(64), nullable=True)
    device: Mapped[str | None] = mapped_column(String(32), nullable=True)
    os: Mapped[str | None] = mapped_column(String(64), nullable=True)
    browser: Mapped[str | None] = mapped_column(String(64), nullable=True)
    region_country: Mapped[str | None] = mapped_column(String(64), nullable=True)
    region_city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    utm_source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    utm_medium: Mapped[str | None] = mapped_column(String(128), nullable=True)
    utm_campaign: Mapped[str | None] = mapped_column(String(256), nullable=True)
    utm_content: Mapped[str | None] = mapped_column(String(256), nullable=True)
    utm_term: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_page_view: Mapped[int] = mapped_column(Integer, default=0)
    is_download: Mapped[int] = mapped_column(Integer, default=0)
    is_link: Mapped[int] = mapped_column(Integer, default=0)
    not_bounce: Mapped[int] = mapped_column(Integer, default=0)
    extra: Mapped[str | None] = mapped_column(Text, nullable=True)


class SerpResult(Base):
    """A single SERP position from the ARSENKIN 'check-top' parser: one row per
    (keyword, search engine, region, position) on a capture date."""

    __tablename__ = "serp_result"
    __table_args__ = (
        UniqueConstraint("keyword", "se", "region", "position", "captured_on", name="uq_serp"),
        Index("ix_serp_kw", "keyword", "se", "captured_on"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    keyword: Mapped[str] = mapped_column(Text)
    se: Mapped[int] = mapped_column(Integer)            # arsenkin se.type (1/2/3/11/12…)
    region: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[int] = mapped_column(Integer)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    url_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_on: Mapped[date_type] = mapped_column(Date, index=True)
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class SerpTask(Base):
    """A submitted ARSENKIN check-top task, tracked so an interrupted web run can
    be resumed ('догрузить недостающие') without re-paying limits. Readiness and
    results are fetched later by ``task_id`` via /check + /get."""

    __tablename__ = "serp_task"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | done
    phrases: Mapped[int] = mapped_column(Integer, default=0)
    stored: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DonorSite(Base):
    """A guest-post donor site from the Miralinks catalog.

    One row per site (keyed by ``source`` + ``external_id`` = Miralinks Ground
    id), refreshed/upserted on each catalog pull. The clean fields the UI filters
    and sorts on are typed; the full original ``rowData`` object is kept in
    ``raw`` so nothing is lost even if a field isn't mapped.
    """

    __tablename__ = "donor_site"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_donor_site"),
        Index("ix_donor_domain", "domain"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default="miralinks", index=True)
    external_id: Mapped[str] = mapped_column(String(64))      # Miralinks Ground.id
    domain: Mapped[str] = mapped_column(String(255))          # registrable (topDomain)
    site_url: Mapped[str | None] = mapped_column(String(512), nullable=True)  # folder_url_wl
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # quality metrics
    sqi: Mapped[int | None] = mapped_column(Integer, nullable=True)        # Яндекс ИКС
    cy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    da: Mapped[int | None] = mapped_column(Integer, nullable=True)         # Moz DA
    ahrefs_dr: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Ahrefs DR
    cf: Mapped[int | None] = mapped_column(Integer, nullable=True)         # Majestic CF
    tf: Mapped[int | None] = mapped_column(Integer, nullable=True)         # Majestic TF
    spamness: Mapped[float | None] = mapped_column(Float, nullable=True)   # Яндекс спам
    indexed_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ya_indexed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    google_indexed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    traffic: Mapped[int | None] = mapped_column(Integer, nullable=True)    # месячный трафик
    traffic_interval: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ahrefs_traffic: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ahrefs_domains: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ahrefs_keywords: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # commercial
    price_rur: Mapped[int | None] = mapped_column(Integer, nullable=True)        # размещение
    price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    article_price_rur: Mapped[int | None] = mapped_column(Integer, nullable=True)  # написание
    # meta
    region_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    region: Mapped[str | None] = mapped_column(String(128), nullable=True)
    topics: Mapped[str | None] = mapped_column(String(512), nullable=True)
    lang: Mapped[str | None] = mapped_column(String(16), nullable=True)
    links_in_articles: Mapped[int | None] = mapped_column(Integer, nullable=True)
    articles_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    placement_time_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_placement: Mapped[str | None] = mapped_column(String(32), nullable=True)
    venality: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_exclusive: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_fast: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_pr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trusted: Mapped[int | None] = mapped_column(Integer, nullable=True)
    screenshot: Mapped[str | None] = mapped_column(String(512), nullable=True)
    raw: Mapped[str | None] = mapped_column(Text, nullable=True)  # full rowData JSON
    captured_on: Mapped[date_type] = mapped_column(Date, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class WordstatHistory(Base):
    """Monthly Wordstat demand (frequency) per query/region — the «История запросов»
    series, collected for our keywords and accumulated over time."""

    __tablename__ = "wordstat_history"
    __table_args__ = (
        UniqueConstraint("query_hash", "region", "device", "date", name="uq_wordstat"),
        Index("ix_wordstat_query", "query_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    query: Mapped[str] = mapped_column(Text)
    query_hash: Mapped[str] = mapped_column(String(40), index=True)
    region: Mapped[str] = mapped_column(String(48), default="all")
    device: Mapped[str] = mapped_column(String(48), default="all")
    match_type: Mapped[str] = mapped_column(String(8), default="broad")  # broad|phrase|exact|order
    date: Mapped[date_type] = mapped_column(Date)   # first day of the month
    value: Mapped[int] = mapped_column(Integer, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class WordstatSeries(Base):
    """Fine-grained Wordstat demand — by day or week (the «Динамика» graph in the
    new Wordstat supports «По дням»/«По неделям»). Kept separate from the monthly
    ``wordstat_history`` so monthly and daily points never collide on the same date.
    ``date`` = the day (day granularity) or the week's start day (week granularity)."""

    __tablename__ = "wordstat_series"
    __table_args__ = (
        UniqueConstraint("query_hash", "region", "device", "granularity", "date",
                         name="uq_wordstat_series"),
        Index("ix_wordstat_series_query", "query_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    query: Mapped[str] = mapped_column(Text)
    query_hash: Mapped[str] = mapped_column(String(40), index=True)
    region: Mapped[str] = mapped_column(String(48), default="all")
    device: Mapped[str] = mapped_column(String(48), default="all")
    granularity: Mapped[str] = mapped_column(String(8), default="day")  # day | week
    match_type: Mapped[str] = mapped_column(String(8), default="broad")  # broad|phrase|exact|order
    date: Mapped[date_type] = mapped_column(Date)
    value: Mapped[int] = mapped_column(Integer, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


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
