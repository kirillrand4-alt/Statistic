"""Per-domain URL → brand map (uploaded CSV) used to filter a project by brand.

The CSV is two columns — brand and URL (any order; tab/comma/semicolon; cp1251
or utf-8, header optional). URLs are keyed by host+path (``page_key``), the same
key the goals matcher uses, so brand URLs line up with the project's URLs even
when one side carries UTM tags / www / a different scheme.
"""
from __future__ import annotations

import csv
import io

from sqlalchemy import delete, distinct, func, select
from sqlalchemy.orm import Session

from app.db.models import ProjectUrl, UrlBrand
from app.services.goals import page_key
from app.utils import domain_of, normalize_url


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def import_brand_csv(db: Session, raw: bytes) -> dict:
    """Replace the brand map for every domain present in the CSV. Returns a
    summary: rows imported, domains, brands."""
    text = _decode(raw)
    sample = text[:4000]
    delim = "\t" if "\t" in sample else (";" if ";" in sample else ",")
    payload: dict[tuple[str, str, str], str] = {}  # (domain,key,brand) -> url
    domains: set[str] = set()
    for row in csv.reader(io.StringIO(text), delimiter=delim):
        if len(row) < 2:
            continue
        a, b = row[0].strip(), row[1].strip()
        if "/" in b or "://" in b:          # col0 = brand, col1 = url
            brand, url = a, b
        elif "/" in a or "://" in a:          # swapped columns
            brand, url = b, a
        else:
            continue                          # header / junk
        dom, key = domain_of(url), page_key(url)
        if not (dom and key and brand):
            continue
        payload[(dom, key, brand)] = url
        domains.add(dom)
    if not payload:
        return {"rows": 0, "domains": [], "brands": 0}

    for dom in domains:  # re-upload replaces that domain's map
        db.execute(delete(UrlBrand).where(UrlBrand.domain == dom))
    rows = [{"domain": d, "url_key": k, "brand": b, "url": u}
            for (d, k, b), u in payload.items()]
    for i in range(0, len(rows), 1000):
        db.execute(UrlBrand.__table__.insert(), rows[i:i + 1000])
    db.commit()
    return {"rows": len(rows), "domains": sorted(domains),
            "brands": len({b for _, _, b in payload})}


def list_brands(db: Session, domain: str) -> list[dict]:
    """[{brand, urls}] for a domain, most URLs first."""
    if not domain:
        return []
    rows = db.execute(
        select(UrlBrand.brand, func.count(distinct(UrlBrand.url_key)))
        .where(UrlBrand.domain == domain).group_by(UrlBrand.brand)
        .order_by(func.count(distinct(UrlBrand.url_key)).desc())
    ).all()
    return [{"brand": b, "urls": int(c)} for b, c in rows]


def sync_project_urls(db: Session, project, domain: str) -> int:
    """Add the domain's brand URLs to ``project`` (so per-URL stats/goals work).
    Returns how many new URLs were added. Existing URLs are left as-is."""
    if not domain:
        return 0
    rows = db.execute(
        select(distinct(UrlBrand.url)).where(UrlBrand.domain == domain)
    ).all()
    existing = {u.normalized_url for u in project.urls}
    batch = []
    for (url,) in rows:
        nu = normalize_url(url)
        if nu in existing:
            continue
        existing.add(nu)
        batch.append({"project_id": project.id, "url": url, "normalized_url": nu})
    for i in range(0, len(batch), 1000):
        db.execute(ProjectUrl.__table__.insert(), batch[i:i + 1000])
    db.commit()
    return len(batch)


def brand_url_keys(db: Session, domain: str, brands: list[str]) -> set[str]:
    """page_key set for the given brands of a domain (for URL filtering)."""
    if not (domain and brands):
        return set()
    rows = db.execute(
        select(distinct(UrlBrand.url_key))
        .where(UrlBrand.domain == domain, UrlBrand.brand.in_(list(brands)))
    ).all()
    return {r[0] for r in rows}
