"""Connect a data source by pasting its API key, then auto-pull data.

Used by the admin UI: the owner pastes a GSC service-account JSON, we save it,
discover the accessible properties, register them, and kick off a background
backfill so statistics appear without any manual steps.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bootstrap import ensure_sources
from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models import Site
from app.providers import get_provider, reset_cache
from app.providers.base import DateRange
from app.scheduler.jobs import collect_site

logger = logging.getLogger(__name__)


def save_gsc_key(json_text: str) -> None:
    """Validate and persist the GSC service-account JSON to the configured path."""
    data = json.loads(json_text)  # raises if not valid JSON
    if "client_email" not in data or "private_key" not in data:
        raise ValueError("Это не похоже на JSON ключа сервисного аккаунта Google.")
    path = Path(get_settings().gsc_service_account_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    reset_cache("gsc")  # rebuild provider with the new credentials


def discover_and_register_sites(db: Session) -> list[Site]:
    gsc = ensure_sources(db)["gsc"]
    provider = get_provider("gsc")
    created: list[Site] = []
    for entry in provider.list_sites():
        url = entry.get("site_url")
        if not url or entry.get("permission") == "siteUnverifiedUser":
            continue
        existing = db.execute(
            select(Site).where(Site.source_id == gsc.id, Site.property_uri == url)
        ).scalar_one_or_none()
        if existing is None:
            site = Site(source_id=gsc.id, property_uri=url, display_name=url, enabled=True)
            db.add(site)
            db.commit()
            created.append(site)
    return created


def _backfill_all(site_ids: list[int], days: int) -> None:
    db = SessionLocal()
    try:
        end = date.today() - timedelta(days=1)
        dr = DateRange(start=end - timedelta(days=days), end=end)
        for sid in site_ids:
            site = db.get(Site, sid)
            if site is None:
                continue
            try:
                collect_site(db, site, dr, job_type="backfill")
            except Exception:  # noqa: BLE001 - logged; surfaced in the runs table
                logger.exception("Auto-backfill failed for site %s", sid)
    finally:
        db.close()


def connect_gsc(db: Session, json_text: str, backfill_days: int = 480, background: bool = True) -> dict:
    """Save the key, register accessible sites, and start pulling data."""
    save_gsc_key(json_text)
    created = discover_and_register_sites(db)

    gsc = ensure_sources(db)["gsc"]
    site_ids = [
        s.id
        for s in db.execute(
            select(Site).where(Site.source_id == gsc.id, Site.enabled.is_(True))
        ).scalars()
    ]

    if site_ids:
        if background:
            threading.Thread(target=_backfill_all, args=(site_ids, backfill_days), daemon=True).start()
        else:
            _backfill_all(site_ids, backfill_days)

    return {"created": [s.property_uri for s in created], "site_ids": site_ids}
