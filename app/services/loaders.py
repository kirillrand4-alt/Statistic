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
    return [pid for (pid,) in db.execute(
        select(Page.id).where(Page.site_id.in_(ids), Page.normalized_url.in_(list(norms)))
    ).all()]


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
    rows = db.execute(
        select(Page.normalized_url, Page.id).where(
            Page.site_id.in_(_idlist(site_id)), Page.normalized_url.in_(norms)
        )
    ).all()
    return {n: pid for n, pid in rows}


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
    rows = db.execute(
        select(Page.id).where(
            Page.site_id.in_(_idlist(site_id)), Page.normalized_url.in_(norms)
        )
    ).all()
    return [r[0] for r in rows]


def load_page_metrics_df(db, site_id, dr: DateRange, page_ids=None) -> pd.DataFrame:
    stmt = (
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
    if page_ids is not None:
        stmt = stmt.where(PageMetricDaily.page_id.in_(list(page_ids)))
    df = pd.DataFrame(db.execute(stmt).all(), columns=PAGE_COLS)
    return _collapse_overlap(df, ["url", "date"]) if len(_idlist(site_id)) > 1 else df


def load_query_metrics_df(db, site_id, dr: DateRange, page_ids=None) -> pd.DataFrame:
    stmt = (
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
    if page_ids is not None:
        stmt = stmt.where(QueryMetricDaily.page_id.in_(list(page_ids)))
    df = pd.DataFrame(db.execute(stmt).all(), columns=QUERY_COLS)
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
    stmt = (
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
    if page_ids is not None:
        stmt = stmt.where(DeviceMetricDaily.page_id.in_(list(page_ids)))
    df = pd.DataFrame(
        db.execute(stmt).all(),
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
