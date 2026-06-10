"""Scheduled & on-demand data collection.

``collect_site`` pulls a date window from a provider and upserts it
idempotently; ``run_daily_collect`` computes an incremental window per site and
always re-fetches the last few days (GSC revises recent data). ``run_backfill``
pulls a long history on demand.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models import CollectionRun, Project, ProjectUrl, Site
from app.providers import get_provider
from app.providers.base import DateRange
from app.services.ingest import (
    upsert_device_metrics,
    upsert_page_metrics,
    upsert_query_metrics,
    upsert_site_totals,
)

logger = logging.getLogger(__name__)
_scheduler = None
SEED_DAYS = 30  # first auto-run window when there is no history


def tracked_urls(db: Session, site: Site) -> list[str]:
    rows = db.execute(
        select(ProjectUrl.url)
        .join(Project, Project.id == ProjectUrl.project_id)
        .where(Project.site_id == site.id)
        .distinct()
    ).all()
    return [u for (u,) in rows]


def last_ok_date(db: Session, site: Site) -> date | None:
    return db.execute(
        select(CollectionRun.target_date)
        .where(
            CollectionRun.site_id == site.id,
            CollectionRun.status == "ok",
            CollectionRun.target_date.is_not(None),
        )
        .order_by(CollectionRun.target_date.desc())
        .limit(1)
    ).scalar_one_or_none()


def compute_window(db: Session, site: Site, today: date, refetch_days: int) -> DateRange:
    end = today - timedelta(days=1)  # today is incomplete
    last = last_ok_date(db, site)
    if last is None:
        start = end - timedelta(days=SEED_DAYS)
    else:
        start = last - timedelta(days=refetch_days - 1)
    if start > end:
        start = end
    return DateRange(start=start, end=end)


def collect_site(db: Session, site: Site, dr: DateRange, job_type: str = "daily") -> int:
    """Pull a window from the site's provider and upsert it. Returns row count."""
    provider = get_provider(site.source.code)

    run = CollectionRun(
        source_id=site.source_id, site_id=site.id, job_type=job_type,
        target_date=dr.end, status="pending",
    )
    db.add(run)
    db.commit()
    run_id = run.id

    try:
        total = 0
        total += upsert_site_totals(db, site, provider.fetch_site_totals(site, dr))
        total += upsert_page_metrics(db, site, provider.fetch_page_metrics(site, dr))
        if "all_query_metrics" in getattr(provider, "capabilities", set()):
            # One pass for the whole site (page+query) — populates TOP-1 data for
            # every page, so data is complete even before any project is created.
            total += upsert_query_metrics(db, site, provider.fetch_all_query_metrics(site, dr))
        else:
            for url in tracked_urls(db, site):
                total += upsert_query_metrics(
                    db, site, provider.fetch_query_metrics_for_url(site, url, dr)
                )
        if "device_metrics" in getattr(provider, "capabilities", set()):
            total += upsert_device_metrics(
                db, site, provider.fetch_page_metrics_by_device(site, dr)
            )
        db.commit()
        run = db.get(CollectionRun, run_id)
        run.status = "ok"
        run.rows_written = total
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        logger.info("Collected %s rows for site %s (%s..%s)", total, site.id, dr.start, dr.end)
        return total
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run = db.get(CollectionRun, run_id)
        if run is not None:
            run.status = "error"
            run.error_text = str(exc)[:2000]
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
        logger.exception("Collection failed for site %s", site.id)
        raise


def run_daily_collect(db: Session) -> dict[int, int]:
    from app.services import indexing

    settings = get_settings()
    today = date.today()
    results: dict[int, int] = {}
    sites = db.execute(select(Site).where(Site.enabled.is_(True))).scalars().all()
    for site in sites:
        dr = compute_window(db, site, today, settings.collect_refetch_days)
        try:
            results[site.id] = collect_site(db, site, dr, job_type="daily")
        except Exception:  # noqa: BLE001 - already logged; continue with other sites
            results[site.id] = -1
        # Accumulate an index snapshot daily where supported (Yandex), so the
        # set of indexed pages builds history beyond the API's live window.
        try:
            if indexing.supports(site):
                indexing.capture_indexed_urls(db, site)
        except Exception:  # noqa: BLE001
            logger.exception("Daily index snapshot failed for site %s", site.id)
    return results


def run_backfill(db: Session, site_id: int, days: int = 480, chunk_days: int = 30) -> int:
    site = db.get(Site, site_id)
    if site is None:
        raise ValueError(f"No site {site_id}")
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days)
    total = 0
    chunk_end = end
    while chunk_end >= start:  # newest chunk first -> recent data (default view) appears first
        chunk_start = max(chunk_end - timedelta(days=chunk_days - 1), start)
        total += collect_site(db, site, DateRange(start=chunk_start, end=chunk_end), job_type="backfill")
        chunk_end = chunk_start - timedelta(days=1)
    return total


# ----- APScheduler wiring -----
def _daily_job():
    db = SessionLocal()
    try:
        run_daily_collect(db)
    finally:
        db.close()


def start_scheduler():
    global _scheduler
    settings = get_settings()
    if not settings.enable_scheduler:
        logger.info("Scheduler disabled (ENABLE_SCHEDULER=false)")
        return None
    from apscheduler.schedulers.background import BackgroundScheduler

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _daily_job, "cron", hour=settings.collect_cron_hour,
        id="daily_collect", max_instances=1, coalesce=True, replace_existing=True,
    )
    _scheduler.start()
    logger.info("Scheduler started (daily collect at hour %s)", settings.collect_cron_hour)
    return _scheduler


def shutdown_scheduler():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
