"""Keyword cannibalization: pages of the same site competing for one query.

Two complementary detectors over data we already collect:

* **GSC performance** (``query_metric_daily``, the ``page × query × day`` slice
  with ``page_id`` set): for a query, ≥2 of our pages got real impressions —
  Google is splitting them. Richest source; covers every query, historically.
* **SERP** (``serp_result`` from arsenkin): ≥2 of our URLs sit in the actual
  TOP for one keyword — direct proof, both Yandex and Google, for parsed phrases.

The homepage ("/") ranks for almost everything, so it can be excluded to cut
noise (``exclude_home``).
"""
from __future__ import annotations

from urllib.parse import urlparse

import pandas as pd
from sqlalchemy import func, select

from app.db.models import Page, Query, QueryMetricDaily, SerpResult
from app.providers.arsenkin import SE_LABELS
from app.providers.base import DateRange
from app.services.exporting import to_download
from app.utils import as_id_list as _ids


def is_home(url: str | None) -> bool:
    """True for a site's root page (path empty or '/')."""
    if not url:
        return False
    try:
        return urlparse(url).path.rstrip("/") == ""
    except ValueError:
        return False


def gsc_cannibalization(db, site_ids, dr: DateRange, *, min_query_impr: int = 30,
                        min_page_impr: int = 10, min_pages: int = 2,
                        max_position: float | None = None, exclude_home: bool = True,
                        limit: int | None = 300) -> list[dict]:
    """Queries where ≥``min_pages`` of our pages compete, worst (most diluted)
    first. Each result carries the primary page and the cannibal pages with their
    clicks / impressions / avg position, plus a landing-page instability count."""
    ids = _ids(site_ids)
    if not ids:
        return []
    clicks = func.sum(QueryMetricDaily.clicks)
    impr = func.sum(QueryMetricDaily.impressions)
    posw = func.sum(QueryMetricDaily.position * QueryMetricDaily.impressions)
    stmt = (
        select(QueryMetricDaily.query_id, Query.text, QueryMetricDaily.page_id,
               Page.url, clicks.label("c"), impr.label("im"), posw.label("pw"))
        .join(Query, Query.id == QueryMetricDaily.query_id)
        .join(Page, Page.id == QueryMetricDaily.page_id)
        .where(QueryMetricDaily.site_id.in_(ids), QueryMetricDaily.page_id.is_not(None),
               QueryMetricDaily.date >= dr.start, QueryMetricDaily.date <= dr.end)
        .group_by(QueryMetricDaily.query_id, Query.text, QueryMetricDaily.page_id, Page.url)
    )
    per_q: dict[int, dict] = {}
    for qid, text, pid, url, c, im, pw in db.execute(stmt).all():
        c, im = int(c or 0), int(im or 0)
        pos = (float(pw) / im) if (im and pw) else 0.0
        if exclude_home and is_home(url):
            continue
        if im < min_page_impr:
            continue
        if max_position is not None and pos and pos > max_position:
            continue
        q = per_q.setdefault(qid, {"query": text, "pages": []})
        q["pages"].append({"page_id": pid, "url": url, "clicks": c,
                           "impressions": im, "position": round(pos, 1)})

    cannibal = {qid: q for qid, q in per_q.items() if len(q["pages"]) >= min_pages}
    # Instability: per day, the page with most impressions; >1 distinct => churn.
    instab = _instability(db, ids, dr, list(cannibal)) if cannibal else {}

    out = []
    for qid, q in cannibal.items():
        pages = sorted(q["pages"], key=lambda p: (p["clicks"], p["impressions"]), reverse=True)
        total_impr = sum(p["impressions"] for p in pages)
        total_clicks = sum(p["clicks"] for p in pages)
        if total_impr < min_query_impr:
            continue
        primary, cannibals = pages[0], pages[1:]
        secondary_impr = sum(p["impressions"] for p in cannibals)
        out.append({
            "query": q["query"], "pages_n": len(pages),
            "total_impr": total_impr, "total_clicks": total_clicks,
            "primary": primary, "cannibals": cannibals,
            "secondary_impr": secondary_impr,
            "secondary_clicks": sum(p["clicks"] for p in cannibals),
            "top10_pages": sum(1 for p in pages if 0 < p["position"] <= 10),
            "instability": instab.get(qid, 1),
        })
    out.sort(key=lambda r: (r["secondary_impr"], r["total_impr"]), reverse=True)
    return out[:limit] if limit else out


def _instability(db, ids, dr: DateRange, query_ids) -> dict[int, int]:
    """For each query, count distinct pages that were the top (by impressions)
    page on any single day in the range — a flip-flopping landing page."""
    if not query_ids:
        return {}
    stmt = (
        select(QueryMetricDaily.query_id, QueryMetricDaily.date,
               QueryMetricDaily.page_id, QueryMetricDaily.impressions)
        .where(QueryMetricDaily.site_id.in_(ids),
               QueryMetricDaily.query_id.in_(query_ids),
               QueryMetricDaily.page_id.is_not(None),
               QueryMetricDaily.date >= dr.start, QueryMetricDaily.date <= dr.end)
    )
    best: dict[tuple, tuple] = {}  # (qid, date) -> (impr, page_id)
    for qid, d, pid, im in db.execute(stmt).all():
        key = (qid, d)
        cur = best.get(key)
        if cur is None or (im or 0) > cur[0]:
            best[key] = (im or 0, pid)
    tops: dict[int, set] = {}
    for (qid, _d), (_im, pid) in best.items():
        tops.setdefault(qid, set()).add(pid)
    return {qid: len(s) for qid, s in tops.items()}


def serp_cannibalization(db, captured_on: str | None, own_domains, se=None,
                         exclude_home: bool = True, limit: int | None = 300) -> list[dict]:
    """From the stored SERP: keywords where ≥2 of our URLs sit in the TOP (per ПС)."""
    cap = captured_on
    if not cap:
        d = db.execute(select(func.max(SerpResult.captured_on))).scalar()
        cap = d.isoformat() if d else None
    if not cap or not own_domains:
        return []
    stmt = select(SerpResult).where(SerpResult.captured_on == cap,
                                    SerpResult.url_domain.in_(list(own_domains)))
    if se:
        stmt = stmt.where(SerpResult.se.in_(list(se)))
    groups: dict[tuple, dict] = {}
    for r in db.execute(stmt).scalars():
        if exclude_home and is_home(r.url):
            continue
        g = groups.setdefault((r.keyword, r.se), {})
        # keep the best (smallest) position per distinct URL
        if r.url not in g or r.position < g[r.url]:
            g[r.url] = r.position
    out = []
    for (kw, se_t), urls in groups.items():
        if len(urls) < 2:
            continue
        items = sorted(({"url": u, "position": p} for u, p in urls.items()),
                       key=lambda x: x["position"])
        out.append({"keyword": kw, "se": se_t, "se_label": SE_LABELS.get(se_t, se_t),
                    "urls_n": len(items), "best": items[0]["position"], "urls": items})
    out.sort(key=lambda r: (r["urls_n"], -r["best"]), reverse=True)
    return out[:limit] if limit else out


def build_export(rows: list[dict], label: str, fmt: str = "csv"):
    """Flatten GSC cannibalization to one row per competing page."""
    data = []
    for r in rows:
        for role, p in [("основная", r["primary"]), *[("каннибал", c) for c in r["cannibals"]]]:
            data.append({
                "Запрос": r["query"], "Роль": role, "URL": p["url"],
                "Позиция": p["position"], "Клики": p["clicks"], "Показы": p["impressions"],
                "Показов по запросу": r["total_impr"], "Страниц-конкурентов": r["pages_n"],
                "В топ-10": r["top10_pages"], "Нестабильность": r["instability"],
            })
    df = pd.DataFrame(data, columns=["Запрос", "Роль", "URL", "Позиция", "Клики", "Показы",
                                     "Показов по запросу", "Страниц-конкурентов", "В топ-10",
                                     "Нестабильность"])
    safe = "".join(c if (c.isascii() and c.isalnum()) else "_" for c in (label or "all"))[:30]
    name = f"cannibalization_{safe.strip('_') or 'all'}"
    return to_download(df, name, fmt, sheet="Каннибализация")
