"""Snapshots of 'pages in the search index' and comparison over time."""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.base import SessionLocal
from app.db.models import IndexedUrlSnapshot, Site
from app.providers import get_provider
from app.utils import domain_of, normalize_url

logger = logging.getLogger(__name__)


def supports(site: Site) -> bool:
    try:
        return "indexed_urls" in get_provider(site.source.code).capabilities
    except Exception:  # noqa: BLE001
        return False


def has_snapshot_today(db: Session, site: Site) -> bool:
    return bool(db.execute(
        select(func.count()).select_from(IndexedUrlSnapshot).where(
            IndexedUrlSnapshot.site_id == site.id,
            IndexedUrlSnapshot.captured_on == date.today(),
        )
    ).scalar())


def ensure_snapshot(db: Session, site: Site) -> bool:
    """Capture today's index snapshot only if it's still missing (so it runs even
    when the regular collect failed, but isn't repeated needlessly). Returns True
    if a snapshot was taken."""
    if not supports(site) or has_snapshot_today(db, site):
        return False
    capture_indexed_urls(db, site)
    return True


def capture_indexed_urls(db: Session, site: Site) -> int:
    """Fetch the current indexed-URL list and store today's snapshot."""
    provider = get_provider(site.source.code)
    if "indexed_urls" not in getattr(provider, "capabilities", set()):
        raise ValueError("Источник не поддерживает выгрузку страниц в индексе.")
    seen: dict[str, dict] = {}
    for item in provider.fetch_indexed_urls(site):
        n = normalize_url(item["url"])
        if n and n not in seen:
            seen[n] = item
    today = date.today()
    db.execute(
        delete(IndexedUrlSnapshot).where(
            IndexedUrlSnapshot.site_id == site.id, IndexedUrlSnapshot.captured_on == today
        )
    )
    db.add_all([
        IndexedUrlSnapshot(
            site_id=site.id, captured_on=today, url=it["url"],
            normalized_url=n, title=(it.get("title") or "")[:512] or None,
        )
        for n, it in seen.items()
    ])
    db.commit()
    return len(seen)


def capture_async(site_id: int) -> None:
    db = SessionLocal()
    try:
        site = db.get(Site, site_id)
        if site is not None:
            capture_indexed_urls(db, site)
    except Exception:  # noqa: BLE001
        logger.exception("Indexed-URL capture failed for site %s", site_id)
    finally:
        db.close()


def list_snapshots(db: Session, site_id: int) -> list[dict]:
    rows = db.execute(
        select(IndexedUrlSnapshot.captured_on, func.count())
        .where(IndexedUrlSnapshot.site_id == site_id)
        .group_by(IndexedUrlSnapshot.captured_on)
        .order_by(IndexedUrlSnapshot.captured_on.desc())
    ).all()
    return [{"date": d.isoformat(), "count": c} for d, c in rows]


def _snapshot_urls(db, site_id, captured_on) -> dict[str, tuple]:
    rows = db.execute(
        select(IndexedUrlSnapshot.normalized_url, IndexedUrlSnapshot.url, IndexedUrlSnapshot.title)
        .where(IndexedUrlSnapshot.site_id == site_id, IndexedUrlSnapshot.captured_on == captured_on)
    ).all()
    return {n: (u, t) for n, u, t in rows}


def compare_snapshots(db, site_id, date_a, date_b) -> dict:
    """date_a = newer, date_b = older. Added = in A not B; removed = in B not A."""
    a = _snapshot_urls(db, site_id, date_a)
    b = _snapshot_urls(db, site_id, date_b)
    added = [{"url": a[n][0], "title": a[n][1]} for n in a if n not in b]
    removed = [{"url": b[n][0], "title": b[n][1]} for n in b if n not in a]
    return {"added": added, "removed": removed, "count_a": len(a), "count_b": len(b)}


def latest_snapshot_date(db, site_id: int):
    """The most recent captured_on for a site, or None if no snapshot exists."""
    return db.execute(
        select(func.max(IndexedUrlSnapshot.captured_on))
        .where(IndexedUrlSnapshot.site_id == site_id)
    ).scalar_one_or_none()


def check_urls(db, site: Site, urls) -> dict | None:
    """Check which of ``urls`` are in the site's latest indexed snapshot.

    Bare paths ("/page") are resolved against the site's domain. Returns None if
    the site has no snapshot yet. Rows are sorted not-indexed-first.
    """
    captured_on = latest_snapshot_date(db, site.id)
    if captured_on is None:
        return None
    snap = _snapshot_urls(db, site.id, captured_on)  # {normalized: (url, title)}
    base = domain_of(site.property_uri)
    rows, seen, in_count = [], set(), 0
    for raw in urls:
        u = (raw or "").strip()
        if not u:
            continue
        cand = f"https://{base}{u}" if u.startswith("/") and base else u
        n = normalize_url(cand)
        if not n or n in seen:
            continue
        seen.add(n)
        hit = snap.get(n)
        if hit:
            in_count += 1
        rows.append({"input": u, "in_index": bool(hit), "title": hit[1] if hit else None})
    rows.sort(key=lambda r: (r["in_index"], r["input"]))
    return {
        "captured_on": captured_on.isoformat(),
        "snapshot_count": len(snap),
        "total": len(rows),
        "in_count": in_count,
        "out_count": len(rows) - in_count,
        "rows": rows,
    }


def count_history(db, site: Site) -> list[dict]:
    try:
        return get_provider(site.source.code).fetch_index_count_history(site)
    except Exception:  # noqa: BLE001
        return []
