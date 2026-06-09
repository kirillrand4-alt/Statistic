"""Write normalized provider rows into the DB with idempotent upserts.

Re-running collection for the same date overwrites the existing row rather than
duplicating it — essential because GSC revises recent days.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    DeviceMetricDaily,
    Page,
    PageMetricDaily,
    Query,
    QueryMetricDaily,
    Site,
    SiteTotalDaily,
)
from app.providers.base import DeviceMetricRow, PageMetricRow, QueryMetricRow, TotalsRow
from app.utils import normalize_url, query_hash

_CHUNK = 500


def _chunked(rows: Sequence[dict]):
    for i in range(0, len(rows), _CHUNK):
        yield rows[i : i + _CHUNK]


def _upsert(db: Session, model, rows: list[dict], index_elements: list[str], update_cols: list[str]) -> int:
    if not rows:
        return 0
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:  # pragma: no cover - other dialects
        raise RuntimeError(f"Upsert not supported for dialect {dialect!r}")

    written = 0
    for chunk in _chunked(rows):
        stmt = insert(model).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=index_elements,
            set_={c: getattr(stmt.excluded, c) for c in update_cols},
        )
        db.execute(stmt)
        written += len(chunk)
    return written


def ensure_pages(db: Session, site: Site, urls: Iterable[str]) -> dict[str, int]:
    """Return {normalized_url -> page_id}, creating missing pages."""
    norm_to_orig: dict[str, str] = {}
    for u in urls:
        n = normalize_url(u)
        norm_to_orig.setdefault(n, u)
    if not norm_to_orig:
        return {}

    existing = db.execute(
        select(Page.normalized_url, Page.id).where(
            Page.site_id == site.id, Page.normalized_url.in_(list(norm_to_orig))
        )
    ).all()
    mapping = {n: pid for n, pid in existing}

    missing = [n for n in norm_to_orig if n not in mapping]
    for n in missing:
        page = Page(site_id=site.id, url=norm_to_orig[n], normalized_url=n)
        db.add(page)
    if missing:
        db.flush()
        for page in db.execute(
            select(Page).where(Page.site_id == site.id, Page.normalized_url.in_(missing))
        ).scalars():
            mapping[page.normalized_url] = page.id
    return mapping


def ensure_queries(db: Session, site: Site, texts: Iterable[str]) -> dict[str, int]:
    """Return {text_hash -> query_id}, creating missing queries."""
    hash_to_text: dict[str, str] = {}
    for t in texts:
        hash_to_text.setdefault(query_hash(t), t)
    if not hash_to_text:
        return {}

    existing = db.execute(
        select(Query.text_hash, Query.id).where(
            Query.site_id == site.id, Query.text_hash.in_(list(hash_to_text))
        )
    ).all()
    mapping = {h: qid for h, qid in existing}

    missing = [h for h in hash_to_text if h not in mapping]
    for h in missing:
        db.add(Query(site_id=site.id, text=hash_to_text[h], text_hash=h))
    if missing:
        db.flush()
        for q in db.execute(
            select(Query).where(Query.site_id == site.id, Query.text_hash.in_(missing))
        ).scalars():
            mapping[q.text_hash] = q.id
    return mapping


def upsert_page_metrics(db: Session, site: Site, rows: Iterable[PageMetricRow]) -> int:
    rows = list(rows)
    page_map = ensure_pages(db, site, (r.url for r in rows))
    payload = [
        {
            "site_id": site.id,
            "page_id": page_map[normalize_url(r.url)],
            "date": r.date,
            "clicks": r.clicks,
            "impressions": r.impressions,
            "ctr": r.ctr,
            "position": r.position,
        }
        for r in rows
    ]
    return _upsert(
        db, PageMetricDaily, payload,
        ["site_id", "page_id", "date"],
        ["clicks", "impressions", "ctr", "position"],
    )


def upsert_query_metrics(db: Session, site: Site, rows: Iterable[QueryMetricRow]) -> int:
    rows = list(rows)
    page_urls = [r.url for r in rows if r.url]
    page_map = ensure_pages(db, site, page_urls)
    query_map = ensure_queries(db, site, (r.query for r in rows))
    payload = [
        {
            "site_id": site.id,
            "page_id": page_map.get(normalize_url(r.url)) if r.url else None,
            "query_id": query_map[query_hash(r.query)],
            "date": r.date,
            "clicks": r.clicks,
            "impressions": r.impressions,
            "ctr": r.ctr,
            "position": r.position,
        }
        for r in rows
    ]
    return _upsert(
        db, QueryMetricDaily, payload,
        ["site_id", "page_id", "query_id", "date"],
        ["clicks", "impressions", "ctr", "position"],
    )


def upsert_device_metrics(db: Session, site: Site, rows: Iterable[DeviceMetricRow]) -> int:
    rows = list(rows)
    page_map = ensure_pages(db, site, (r.url for r in rows))
    payload = [
        {
            "site_id": site.id,
            "page_id": page_map[normalize_url(r.url)],
            "date": r.date,
            "device": r.device,
            "clicks": r.clicks,
            "impressions": r.impressions,
            "ctr": r.ctr,
            "position": r.position,
        }
        for r in rows
    ]
    return _upsert(
        db, DeviceMetricDaily, payload,
        ["site_id", "page_id", "date", "device"],
        ["clicks", "impressions", "ctr", "position"],
    )


def upsert_site_totals(db: Session, site: Site, rows: Iterable[TotalsRow]) -> int:
    payload = [
        {
            "site_id": site.id,
            "date": r.date,
            "clicks": r.clicks,
            "impressions": r.impressions,
            "ctr": r.ctr,
            "position": r.position,
        }
        for r in rows
    ]
    return _upsert(
        db, SiteTotalDaily, payload,
        ["site_id", "date"],
        ["clicks", "impressions", "ctr", "position"],
    )
