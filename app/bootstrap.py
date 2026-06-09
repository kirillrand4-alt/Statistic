"""Idempotent startup bootstrap: ensure source rows and the default site."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Site, Source

logger = logging.getLogger(__name__)

SOURCES = [
    ("gsc", "Google Search Console"),
    ("yandex_webmaster", "Яндекс.Вебмастер"),
    ("yandex_metrika", "Яндекс.Метрика"),
]


def ensure_sources(db: Session) -> dict[str, Source]:
    existing = {s.code: s for s in db.execute(select(Source)).scalars()}
    for code, name in SOURCES:
        if code not in existing:
            src = Source(code=code, name=name)
            db.add(src)
            existing[code] = src
    db.commit()
    return existing


def ensure_default_site(db: Session) -> Site | None:
    settings = get_settings()
    if not settings.gsc_site_url:
        return None
    sources = ensure_sources(db)
    gsc = sources["gsc"]
    site = db.execute(
        select(Site).where(
            Site.source_id == gsc.id, Site.property_uri == settings.gsc_site_url
        )
    ).scalar_one_or_none()
    if site is None:
        site = Site(
            source_id=gsc.id,
            property_uri=settings.gsc_site_url,
            display_name=settings.gsc_site_url,
            enabled=True,
        )
        db.add(site)
        db.commit()
        logger.info("Registered default GSC site: %s", settings.gsc_site_url)
    return site


def bootstrap(db: Session) -> None:
    ensure_sources(db)
    ensure_default_site(db)
