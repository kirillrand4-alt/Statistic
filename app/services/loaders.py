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

PAGE_COLS = ["url", "date", "clicks", "impressions", "position"]
QUERY_COLS = ["url", "query", "date", "clicks", "impressions", "position"]
TOTAL_COLS = ["date", "clicks", "impressions", "position"]
AGG_OUT = ["clicks", "impressions", "ctr", "position"]


def _idlist(site_id) -> list[int]:
    """Accept a single site_id or a list of them (for merging same-domain properties)."""
    if isinstance(site_id, (list, tuple, set)):
        return list(site_id)
    return [site_id]


def project_page_id_map(db: Session, site_id: int, project: Project) -> dict[str, int]:
    norms = [u.normalized_url for u in project.urls]
    if not norms:
        return {}
    rows = db.execute(
        select(Page.normalized_url, Page.id).where(
            Page.site_id == site_id, Page.normalized_url.in_(norms)
        )
    ).all()
    return {n: pid for n, pid in rows}


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
    return pd.DataFrame(db.execute(stmt).all(), columns=PAGE_COLS)


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
    return pd.DataFrame(db.execute(stmt).all(), columns=QUERY_COLS)


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
    return pd.DataFrame(db.execute(stmt).all(), columns=TOTAL_COLS)


def load_device_metrics_df(db, site_id, dr: DateRange, page_ids=None) -> pd.DataFrame:
    stmt = (
        select(
            Page.url,
            DeviceMetricDaily.device,
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
    return pd.DataFrame(
        db.execute(stmt).all(), columns=["url", "device", "clicks", "impressions", "position"]
    )


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
