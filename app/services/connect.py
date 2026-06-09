"""Connect Google Search Console by pasting credentials, then auto-pull data.

Two credential types are supported (both stored encrypted via app.credentials):
- OAuth: client_id + client_secret + refresh_token (long-lived access)
- Service account: the JSON key

After saving, we discover the accessible properties, register them, and start a
background backfill so statistics appear without any manual steps.
"""
from __future__ import annotations

import json
import logging
import threading

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bootstrap import ensure_sources
from app.credentials import set_cred
from app.db.base import SessionLocal
from app.db.models import Site
from app.providers import get_provider, reset_cache
from app.scheduler.jobs import run_backfill

logger = logging.getLogger(__name__)


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
            db.add(Site(source_id=gsc.id, property_uri=url, display_name=url, enabled=True))
            db.commit()
            created.append(
                db.execute(
                    select(Site).where(Site.source_id == gsc.id, Site.property_uri == url)
                ).scalar_one()
            )
    return created


def _backfill_all(site_ids: list[int], days: int) -> None:
    db = SessionLocal()
    try:
        for sid in site_ids:
            try:
                run_backfill(db, sid, days)  # chunked: commits every ~30 days
            except Exception:  # noqa: BLE001 - logged; visible in the runs table
                logger.exception("Auto-backfill failed for site %s", sid)
    finally:
        db.close()


def _finalize(db: Session, backfill_days: int, background: bool) -> dict:
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
            threading.Thread(
                target=_backfill_all, args=(site_ids, backfill_days), daemon=True
            ).start()
        else:
            _backfill_all(site_ids, backfill_days)
    return {"created": [s.property_uri for s in created], "site_ids": site_ids}


def connect_gsc_service_account(
    db: Session, json_text: str, backfill_days: int = 480, background: bool = True
) -> dict:
    data = json.loads(json_text)
    if "client_email" not in data or "private_key" not in data:
        raise ValueError("Это не похоже на JSON ключа сервисного аккаунта Google.")
    set_cred("gsc_auth_mode", "service_account")
    set_cred("gsc_sa_json", json.dumps(data))
    reset_cache("gsc")
    return _finalize(db, backfill_days, background)


def connect_gsc_oauth(
    db: Session,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    backfill_days: int = 480,
    background: bool = True,
) -> dict:
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    refresh_token = (refresh_token or "").strip()
    if not (client_id and client_secret and refresh_token):
        raise ValueError("Нужны client_id, client_secret и refresh_token.")
    set_cred("gsc_auth_mode", "oauth")
    set_cred("gsc_oauth_client_id", client_id)
    set_cred("gsc_oauth_client_secret", client_secret)
    set_cred("gsc_oauth_refresh_token", refresh_token)
    reset_cache("gsc")
    return _finalize(db, backfill_days, background)
