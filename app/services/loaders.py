"""Load metric rows from the DB into pandas DataFrames + shared aggregation.

CTR and average position are always recomputed from summed components
(clicks/impressions, impression-weighted position) — never by averaging daily
CTRs/positions — so aggregates over any period are correct.
"""
from __future__ import annotations

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    DeviceMetricDaily,
    Page,
    PageMetricDaily,
    Project,
    Query,
    QueryMetricDaily,
    SiteTotalDaily,
)
from app.providers.base import DateRange
from app.utils import as_id_list, normalize_url

PAGE_COLS = ["url", "date", "clicks", "impressions", "position"]
QUERY_COLS = ["url", "query", "date", "clicks", "impressions", "position"]
TOTAL_COLS = ["date", "clicks", "impressions", "position"]
AGG_OUT = ["clicks", "impressions", "ctr", "position"]


def _idlist(site_id) -> list[int]:
    """Accept a single site_id or a list of them (for merging same-domain properties)."""
    return as_id_list(site_id)


# Chunk a large `page_id IN (...)` list so it never exceeds SQLite's bound-variable
# limit (999 on older builds, 32766 on 3.32+). Big projects — especially when
# merging same-domain properties multiplies page_ids — otherwise overflow it and
# the metrics query fails/returns wrong totals. Kept well under the oldest limit
# (999), leaving headroom for the site_id + date bind params in the same query.
_VAR_CHUNK = 800


def _fetch(db, base_stmt, id_col, page_ids):
    """Run ``base_stmt``; if ``page_ids`` is given, filter by ``id_col IN page_ids``
    in chunks and concatenate rows (each id falls in exactly one chunk, so sums are
    exact). ``page_ids=None`` = no id filter; empty list = no rows."""
    if page_ids is None:
        return db.execute(base_stmt).all()
    ids = list(page_ids)
    if not ids:
        return []
    if len(ids) <= _VAR_CHUNK:
        return db.execute(base_stmt.where(id_col.in_(ids))).all()
    rows = []
    for i in range(0, len(ids), _VAR_CHUNK):
        rows.extend(db.execute(base_stmt.where(id_col.in_(ids[i:i + _VAR_CHUNK]))).all())
    return rows



def resolve_page_ids(db, site_ids, urls) -> list[int]:
    """page_ids for the given URLs within ``site_ids``, matched by normalized URL
    regardless of http/https, www or trailing slash."""
    norms: set[str] = set()
    for u in urls or []:
        if not u:
            continue
        n = normalize_url(u)
        norms.add(n)
        if n.startswith("https://"):       # match regardless of stored scheme
            norms.add("http://" + n[len("https://"):])
        elif n.startswith("http://"):
            norms.add("https://" + n[len("http://"):])
    ids = as_id_list(site_ids)
    if not ids or not norms:
        return []
    base = select(Page.id).where(Page.site_id.in_(ids))
    return [pid for (pid,) in _fetch(db, base, Page.normalized_url, list(norms))]


def _collapse_overlap(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Merge same-domain properties: keep one row per ``keys`` group — the most
    complete (max impressions). A GSC domain property already includes its
    https:// prefix property, so overlapping (URL, day) rows must not be summed.
    Distinct rows (different URL/query/day, or http vs https) keep their own keys.
    """
    if df.empty:
        return df
    return df.sort_values("impressions").groupby(keys, as_index=False, dropna=False).last()


def project_page_id_map(db: Session, site_id: int, project: Project) -> dict[str, int]:
    norms = [u.normalized_url for u in project.urls]
    if not norms:
        return {}
    base = select(Page.normalized_url, Page.id).where(Page.site_id.in_(_idlist(site_id)))
    return {n: pid for n, pid in _fetch(db, base, Page.normalized_url, norms)}


def project_page_ids(db: Session, site_id, project: Project, only_norms=None) -> list[int]:
    """All Page ids matching the project's URLs across one or several sites
    (so a project resolves to its pages in every merged same-domain property).
    ``only_norms`` (a set of normalized URLs) restricts to that subset — used to
    filter the project by brand."""
    norms = [u.normalized_url for u in project.urls]
    if only_norms is not None:
        norms = [n for n in norms if n in only_norms]
    if not norms:
        return []
    base = select(Page.id).where(Page.site_id.in_(_idlist(site_id)))
    return [r[0] for r in _fetch(db, base, Page.normalized_url, norms)]


def load_page_metrics_df(db, site_id, dr: DateRange, page_ids=None) -> pd.DataFrame:
    base = (
        select(
            Page.url,
            PageMetricDaily.date,
            PageMetricDaily.clicks,
            PageMetricDaily.impressions,
            PageMetricDaily.position,
        )
        .join(Page, Page.id == PageMetricDaily.page_id)
        .where(
            PageMetricDaily.site_id.in_(_idlist(site_id)),
            PageMetricDaily.date >= dr.start,
            PageMetricDaily.date <= dr.end,
        )
    )
    df = pd.DataFrame(_fetch(db, base, PageMetricDaily.page_id, page_ids), columns=PAGE_COLS)
    return _collapse_overlap(df, ["url", "date"]) if len(_idlist(site_id)) > 1 else df


def load_query_metrics_df(db, site_id, dr: DateRange, page_ids=None) -> pd.DataFrame:
    base = (
        select(
            Page.url,
            Query.text.label("query"),
            QueryMetricDaily.date,
            QueryMetricDaily.clicks,
            QueryMetricDaily.impressions,
            QueryMetricDaily.position,
        )
        .join(Query, Query.id == QueryMetricDaily.query_id)
        .join(Page, Page.id == QueryMetricDaily.page_id, isouter=True)
        .where(
            QueryMetricDaily.site_id.in_(_idlist(site_id)),
            QueryMetricDaily.date >= dr.start,
            QueryMetricDaily.date <= dr.end,
        )
    )
    df = pd.DataFrame(_fetch(db, base, QueryMetricDaily.page_id, page_ids), columns=QUERY_COLS)
    return _collapse_overlap(df, ["url", "query", "date"]) if len(_idlist(site_id)) > 1 else df


def load_site_totals_df(db, site_id, dr: DateRange) -> pd.DataFrame:
    stmt = select(
        SiteTotalDaily.date,
        SiteTotalDaily.clicks,
        SiteTotalDaily.impressions,
        SiteTotalDaily.position,
    ).where(
        SiteTotalDaily.site_id.in_(_idlist(site_id)),
        SiteTotalDaily.date >= dr.start,
        SiteTotalDaily.date <= dr.end,
    )
    df = pd.DataFrame(db.execute(stmt).all(), columns=TOTAL_COLS)
    return _collapse_overlap(df, ["date"]) if len(_idlist(site_id)) > 1 else df


def load_device_metrics_df(db, site_id, dr: DateRange, page_ids=None) -> pd.DataFrame:
    base = (
        select(
            Page.url,
            DeviceMetricDaily.device,
            DeviceMetricDaily.date,
            DeviceMetricDaily.clicks,
            DeviceMetricDaily.impressions,
            DeviceMetricDaily.position,
        )
        .join(Page, Page.id == DeviceMetricDaily.page_id)
        .where(
            DeviceMetricDaily.site_id.in_(_idlist(site_id)),
            DeviceMetricDaily.date >= dr.start,
            DeviceMetricDaily.date <= dr.end,
        )
    )
    df = pd.DataFrame(
        _fetch(db, base, DeviceMetricDaily.page_id, page_ids),
        columns=["url", "device", "date", "clicks", "impressions", "position"],
    )
    return _collapse_overlap(df, ["url", "device", "date"]) if len(_idlist(site_id)) > 1 else df


def agg_metrics(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    """Aggregate to summed clicks/impressions, recomputed CTR, weighted position."""
    if df.empty:
        return pd.DataFrame(columns=group_cols + AGG_OUT)
    df = df.copy()
    df["_pos_imp"] = df["position"] * df["impressions"]
    g = df.groupby(group_cols, as_index=False).agg(
        clicks=("clicks", "sum"),
        impressions=("impressions", "sum"),
        _pos_imp=("_pos_imp", "sum"),
    )
    imp = g["impressions"].replace(0, pd.NA)
    g["ctr"] = (g["clicks"] / imp).fillna(0.0)
    g["position"] = (g["_pos_imp"] / imp).fillna(0.0)
    return g.drop(columns=["_pos_imp"])


def totals_from_df(df: pd.DataFrame) -> dict:
    """Collapse a metrics DataFrame to a single totals dict."""
    clicks = int(df["clicks"].sum()) if not df.empty else 0
    impressions = int(df["impressions"].sum()) if not df.empty else 0
    if not df.empty and impressions:
        position = float((df["position"] * df["impressions"]).sum() / impressions)
    else:
        position = 0.0
    ctr = clicks / impressions if impressions else 0.0
    return {"clicks": clicks, "impressions": impressions, "ctr": ctr, "position": position}
