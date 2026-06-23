"""Scheduled & on-demand data collection.

``collect_site`` pulls a date window from a provider and upserts it
idempotently; ``run_daily_collect`` computes an incremental window per site and
always re-fetches the last few days (GSC revises recent data). ``run_backfill``
pulls a long history on demand.
"""
from __future__ import annotations

import logging
import threading
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
        # Yandex "pages in index" snapshot — capture on EVERY collect (dashboard
        # «Собрать», admin, or the scheduler), so it stays fresh on any path, not
        # only the nightly job. A snapshot failure must not fail the collection.
        try:
            from app.services import indexing
            if indexing.supports(site):
                indexing.capture_indexed_urls(db, site)
        except Exception:  # noqa: BLE001
            logger.exception("Index snapshot failed for site %s", site.id)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
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
    settings = get_settings()
    today = date.today()
    results: dict[int, int] = {}
    sites = db.execute(select(Site).where(Site.enabled.is_(True))).scalars().all()
    for site in sites:
        dr = compute_window(db, site, today, settings.collect_refetch_days)
        try:
            # collect_site also captures the Yandex index snapshot (where supported)
            results[site.id] = collect_site(db, site, dr, job_type="daily")
        except Exception:  # noqa: BLE001 - already logged; continue with other sites
            results[site.id] = -1
        _ensure_snapshot(db, site)  # snapshot even if the stats collect above failed
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
def _ensure_snapshot(db: Session, site: Site) -> None:
    """Capture today's Yandex index snapshot if it's missing — independent of the
    stats collect, so a failed stats pull doesn't also skip the snapshot."""
    try:
        from app.services import indexing
        indexing.ensure_snapshot(db, site)
    except Exception:  # noqa: BLE001
        logger.exception("Index snapshot failed for site %s", site.id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def last_daily_ok(db: Session) -> datetime | None:
    """When the last successful daily collect (any site) finished — for the admin."""
    dt = db.execute(
        select(CollectionRun.finished_at).where(
            CollectionRun.job_type == "daily",
            CollectionRun.status == "ok",
            CollectionRun.finished_at.is_not(None),
        ).order_by(CollectionRun.finished_at.desc()).limit(1)
    ).scalar_one_or_none()
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _last_ok_for_site(db: Session, site_id: int) -> datetime | None:
    dt = db.execute(
        select(CollectionRun.finished_at).where(
            CollectionRun.site_id == site_id,
            CollectionRun.status == "ok",
            CollectionRun.finished_at.is_not(None),
        ).order_by(CollectionRun.finished_at.desc()).limit(1)
    ).scalar_one_or_none()
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def stale_site_ids(db: Session, min_hours: int = 20) -> list[int]:
    """Enabled sites whose last successful collect is older than min_hours (or never)
    — checked PER SITE, so one fresh site doesn't mask another that's behind."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=min_hours)
    ids = db.execute(select(Site.id).where(Site.enabled.is_(True))).scalars().all()
    return [sid for sid in ids
            if (_last_ok_for_site(db, sid) or datetime.min.replace(tzinfo=timezone.utc)) < cutoff]


def _collect_sites_in_thread(site_ids: list[int]) -> None:
    def _r():
        d = SessionLocal()
        try:
            settings = get_settings()
            today = date.today()
            for sid in site_ids:
                site = d.get(Site, sid)
                if site is None or not site.enabled:
                    continue
                try:
                    dr = compute_window(d, site, today, settings.collect_refetch_days)
                    collect_site(d, site, dr, job_type="daily")
                except Exception:  # noqa: BLE001
                    logger.exception("Catch-up collect failed for site %s", sid)
                _ensure_snapshot(d, site)
        finally:
            d.close()
    threading.Thread(target=_r, daemon=True).start()


def _daily_job():
    """Primary cron run — collect every enabled site (in a worker thread)."""
    def _r():
        d = SessionLocal()
        try:
            run_daily_collect(d)
        except Exception:  # noqa: BLE001
            logger.exception("Daily collect failed")
        finally:
            d.close()
    threading.Thread(target=_r, daemon=True).start()


def _catchup_job():
    """Safety net (interval): collect any site whose data is overdue, so a missed
    cron OR a single failing/stale site is recovered within a few hours."""
    db = SessionLocal()
    try:
        stale = stale_site_ids(db)
    finally:
        db.close()
    if stale:
        logger.info("Scheduler catch-up: %d site(s) overdue, collecting", len(stale))
        _collect_sites_in_thread(stale)


def catch_up_if_overdue() -> bool:
    """At startup: collect each site whose data is overdue (per-site), in background."""
    db = SessionLocal()
    try:
        stale = stale_site_ids(db)
    finally:
        db.close()
    if stale:
        logger.info("Startup catch-up: %d site(s) overdue, collecting", len(stale))
        _collect_sites_in_thread(stale)
        return True
    return False


def scheduler_status() -> dict:
    """Live scheduler state for the admin page (running? next daily run?)."""
    out = {"running": False, "next_run": None}
    if _scheduler is not None:
        try:
            out["running"] = bool(_scheduler.running)
            job = _scheduler.get_job("daily_collect")
            if job is not None and job.next_run_time is not None:
                out["next_run"] = job.next_run_time.isoformat(timespec="minutes")
        except Exception:  # noqa: BLE001
            pass
    return out


def start_scheduler():
    global _scheduler
    settings = get_settings()
    if not settings.enable_scheduler:
        logger.info("Scheduler disabled (ENABLE_SCHEDULER=false)")
        return None
    from apscheduler.schedulers.background import BackgroundScheduler

    try:
        _scheduler = BackgroundScheduler(timezone=settings.timezone or "UTC")
    except Exception:  # noqa: BLE001 - bad tz name -> machine local/UTC
        _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _daily_job, "cron", hour=settings.collect_cron_hour,
        id="daily_collect", max_instances=1, coalesce=True,
        misfire_grace_time=6 * 3600, replace_existing=True,
    )
    # Safety net: if the cron was missed (server off at that hour / restart), re-check
    # every 6h and run when no successful daily collect happened in ~20h.
    _scheduler.add_job(
        _catchup_job, "interval", hours=6, id="daily_catchup",
        max_instances=1, coalesce=True, replace_existing=True,
    )
    _scheduler.start()
    logger.info("Scheduler started (daily collect at hour %s %s; +6h catch-up)",
                settings.collect_cron_hour, settings.timezone)
    return _scheduler


def shutdown_scheduler():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
