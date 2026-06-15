"""Export every enabled site at once over the whole collected period."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import PageMetricDaily, Site, SiteTotalDaily
from app.providers.base import DateRange
from app.services.export import _prettify
from app.services.exporting import to_download
from app.services.loaders import (
    agg_metrics,
    load_page_metrics_df,
    load_query_metrics_df,
    load_site_totals_df,
)

SRC_LABELS = {"gsc": "Google", "yandex_webmaster": "Яндекс", "yandex_metrika": "Метрика"}
LEVELS = ("totals", "page", "query")


def overall_range(db: Session) -> DateRange:
    """Min..max date present across page metrics (fallback: site totals)."""
    lo, hi = db.execute(select(func.min(PageMetricDaily.date), func.max(PageMetricDaily.date))).one()
    if lo is None:
        lo, hi = db.execute(
            select(func.min(SiteTotalDaily.date), func.max(SiteTotalDaily.date))
        ).one()
    if lo is None:
        hi = date.today() - timedelta(days=1)
        lo = hi - timedelta(days=27)
    return DateRange(start=lo, end=hi)


def _site_frame(db, site, dr: DateRange, level: str) -> pd.DataFrame:
    if level == "totals":
        df = load_site_totals_df(db, site.id, dr)
        if df.empty:
            return df
        df = df.sort_values("date")
        df["ctr"] = (df["clicks"] / df["impressions"].replace(0, pd.NA)).fillna(0.0)
        return df[["date", "clicks", "impressions", "ctr", "position"]]
    if level == "page":
        return agg_metrics(load_page_metrics_df(db, site.id, dr), ["url"])
    if level == "query":
        return agg_metrics(load_query_metrics_df(db, site.id, dr), ["url", "query"])
    return pd.DataFrame()


def bulk_dataframe(db, dr: DateRange, level: str) -> pd.DataFrame:
    sites = db.execute(select(Site).where(Site.enabled.is_(True)).order_by(Site.id)).scalars().all()
    frames = []
    for s in sites:
        df = _site_frame(db, s, dr, level)
        if df is None or df.empty:
            continue
        df = df.copy()
        df.insert(0, "Сайт", s.property_uri)
        df.insert(0, "Источник", SRC_LABELS.get(s.source.code, s.source.code))
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def build_bulk_export(db, dr: DateRange, level: str = "page", fmt: str = "xlsx"):
    level = level if level in LEVELS else "page"
    df = _prettify(bulk_dataframe(db, dr, level))
    base = f"all_sites_{level}_{dr.start.isoformat()}_{dr.end.isoformat()}"
    return to_download(df, base, fmt, sheet=level)
