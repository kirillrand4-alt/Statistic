"""Feature 3: total clicks/impressions — site-wide, per-page, and subset."""
from __future__ import annotations

from app.providers.base import DateRange
from app.services.loaders import (
    agg_metrics,
    load_page_metrics_df,
    load_site_totals_df,
    project_page_id_map,
    totals_from_df,
)


def site_totals(db, site_id: int, dr: DateRange) -> dict:
    df = load_site_totals_df(db, site_id, dr)
    return totals_from_df(df)


def site_daily(db, site_id: int, dr: DateRange) -> list[dict]:
    """Daily site totals, for time-series charts."""
    df = load_site_totals_df(db, site_id, dr)
    if df.empty:
        return []
    df = df.sort_values("date")
    out = []
    for _, r in df.iterrows():
        imp = int(r["impressions"])
        out.append(
            {
                "date": r["date"].isoformat(),
                "clicks": int(r["clicks"]),
                "impressions": imp,
                "ctr": (int(r["clicks"]) / imp) if imp else 0.0,
                "position": float(r["position"]),
            }
        )
    return out


def per_page_totals(db, site_id: int, dr: DateRange, page_ids=None) -> list[dict]:
    df = load_page_metrics_df(db, site_id, dr, page_ids=page_ids)
    agg = agg_metrics(df, ["url"])
    agg = agg.sort_values("clicks", ascending=False) if not agg.empty else agg
    return [
        {
            "url": r["url"],
            "clicks": int(r["clicks"]),
            "impressions": int(r["impressions"]),
            "ctr": float(r["ctr"]),
            "position": float(r["position"]),
        }
        for _, r in agg.iterrows()
    ]


def subset_totals(db, project, dr: DateRange) -> dict:
    page_map = project_page_id_map(db, project.site_id, project)
    df = load_page_metrics_df(db, project.site_id, dr, page_ids=list(page_map.values()))
    return totals_from_df(df)
