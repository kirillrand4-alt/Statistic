"""All search keywords (queries) people used to reach the site(s).

Source: the GSC + Yandex Webmaster query data we already collect — the
``page × query × day`` slice in ``Query`` / ``QueryMetricDaily``. Aggregated per
keyword across all pages, days and (selected) properties, so you get the full
keyword list with clicks / impressions / CTR / average position.

Note: search engines don't report ultra-rare or anonymised queries, so "all"
means every keyword the engines actually returned for the period.
"""
from __future__ import annotations

import io
from collections.abc import Iterable

import pandas as pd
from sqlalchemy import func, select

from app.db.models import Page, Query, QueryMetricDaily
from app.providers.base import DateRange
from app.utils import normalize_url

CSV_MEDIA = "text/csv"
XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _ids(site_ids):
    if isinstance(site_ids, Iterable) and not isinstance(site_ids, (str, bytes)):
        return list(site_ids)
    return [site_ids]


def keyword_rows(db, site_ids, dr: DateRange, *, min_clicks: int = 0, min_impr: int = 0,
                 search: str | None = None, limit: int | None = None) -> list[dict]:
    """One row per keyword (summed across pages/days/properties), clicks desc.

    The same keyword reported under several properties (https:// + sc-domain:,
    Google + Yandex) is merged by its text — fine for a keyword list (cross-
    engine totals add up; same-engine overlap may slightly inflate, pick one
    engine to avoid it)."""
    ids = _ids(site_ids)
    if not ids:
        return []
    clicks = func.sum(QueryMetricDaily.clicks)
    impr = func.sum(QueryMetricDaily.impressions)
    posw = func.sum(QueryMetricDaily.position * QueryMetricDaily.impressions)
    stmt = (
        select(Query.text, clicks.label("clicks"), impr.label("impr"), posw.label("posw"))
        .join(Query, Query.id == QueryMetricDaily.query_id)
        .where(QueryMetricDaily.site_id.in_(ids),
               QueryMetricDaily.date >= dr.start, QueryMetricDaily.date <= dr.end)
    )
    if search:
        stmt = stmt.where(Query.text.ilike(f"%{search}%"))
    stmt = stmt.group_by(Query.text)
    if min_clicks:
        stmt = stmt.having(clicks >= min_clicks)
    if min_impr:
        stmt = stmt.having(impr >= min_impr)
    stmt = stmt.order_by(clicks.desc(), impr.desc())
    if limit:
        stmt = stmt.limit(limit)
    out = []
    for text, c, im, pw in db.execute(stmt).all():
        c, im = int(c or 0), int(im or 0)
        out.append({"query": text, "clicks": c, "impressions": im,
                    "ctr": (c / im) if im else 0.0,
                    "position": (float(pw) / im) if (im and pw) else 0.0})
    return out


def keywords_for_urls(db, site_ids, urls, dr: DateRange, *, min_clicks: int = 0,
                      min_impr: int = 0, limit: int | None = None) -> list[str]:
    """Keywords (queries) that the given URLs got from search — straight from the
    collected ``query × page`` data. URLs are matched to stored pages by
    normalized URL (http/https, www, trailing slash agnostic)."""
    ids = _ids(site_ids)
    if not ids or not urls:
        return []
    norms: set[str] = set()
    for u in urls:
        if not u:
            continue
        n = normalize_url(u)
        norms.add(n)
        if n.startswith("https://"):       # match regardless of stored scheme
            norms.add("http://" + n[len("https://"):])
        elif n.startswith("http://"):
            norms.add("https://" + n[len("http://"):])
    page_ids = [pid for (pid,) in db.execute(
        select(Page.id).where(Page.site_id.in_(ids), Page.normalized_url.in_(list(norms)))
    ).all()]
    if not page_ids:
        return []
    clicks = func.sum(QueryMetricDaily.clicks)
    impr = func.sum(QueryMetricDaily.impressions)
    stmt = (
        select(Query.text, clicks.label("c"), impr.label("im"))
        .join(Query, Query.id == QueryMetricDaily.query_id)
        .where(QueryMetricDaily.site_id.in_(ids), QueryMetricDaily.page_id.in_(page_ids),
               QueryMetricDaily.date >= dr.start, QueryMetricDaily.date <= dr.end)
        .group_by(Query.text)
    )
    if min_clicks:
        stmt = stmt.having(clicks >= min_clicks)
    if min_impr:
        stmt = stmt.having(impr >= min_impr)
    stmt = stmt.order_by(clicks.desc(), impr.desc())
    if limit:
        stmt = stmt.limit(limit)
    seen, out = set(), []
    for text, _c, _im in db.execute(stmt).all():
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def keyword_summary(db, site_ids, dr: DateRange) -> dict:
    """Distinct-keyword count + total clicks/impressions over the period."""
    ids = _ids(site_ids)
    if not ids:
        return {"keywords": 0, "clicks": 0, "impressions": 0}
    where = (QueryMetricDaily.site_id.in_(ids),
             QueryMetricDaily.date >= dr.start, QueryMetricDaily.date <= dr.end)
    n = db.execute(
        select(func.count(func.distinct(Query.text)))
        .select_from(QueryMetricDaily).join(Query, Query.id == QueryMetricDaily.query_id)
        .where(*where)
    ).scalar() or 0
    c, im = db.execute(
        select(func.coalesce(func.sum(QueryMetricDaily.clicks), 0),
               func.coalesce(func.sum(QueryMetricDaily.impressions), 0)).where(*where)
    ).one()
    return {"keywords": int(n), "clicks": int(c or 0), "impressions": int(im or 0)}


def build_keyword_export(rows: list[dict], label: str, dr: DateRange, fmt: str = "csv"):
    """(filename, BytesIO, media_type) with every keyword row."""
    df = pd.DataFrame(
        [{"Запрос": r["query"], "Клики": r["clicks"], "Показы": r["impressions"],
          "CTR %": round(r["ctr"] * 100, 2), "Позиция": round(r["position"], 2)} for r in rows],
        columns=["Запрос", "Клики", "Показы", "CTR %", "Позиция"],
    )
    safe = "".join(c if (c.isascii() and c.isalnum()) else "_" for c in (label or "all"))[:40]
    name = f"keywords_{safe.strip('_') or 'all'}_{dr.start}_{dr.end}"
    buf = io.BytesIO()
    if fmt == "csv":
        buf.write(df.to_csv(index=False).encode("utf-8-sig"))
        buf.seek(0)
        return f"{name}.csv", buf, CSV_MEDIA
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="Ключевые слова", index=False)
    buf.seek(0)
    return f"{name}.xlsx", buf, XLSX_MEDIA
