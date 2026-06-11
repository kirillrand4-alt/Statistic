"""Feature 2: CTR per page for a project (+ project aggregate)."""
from __future__ import annotations

from app.providers.base import DateRange
from app.services.loaders import (
    agg_metrics,
    load_page_metrics_df,
    project_page_ids,
    totals_from_df,
)
from app.utils import normalize_url


def ctr_for_project(db, project, dr: DateRange, site_ids=None) -> dict:
    ids = site_ids or project.site_id
    df = load_page_metrics_df(db, ids, dr, page_ids=project_page_ids(db, ids, project))

    by_norm: dict[str, dict] = {}
    if not df.empty:
        agg = agg_metrics(df, ["url"])
        agg["_norm"] = agg["url"].map(normalize_url)
        for _, row in agg.iterrows():
            by_norm[row["_norm"]] = row

    pages = []
    for pu in project.urls:
        row = by_norm.get(pu.normalized_url)
        if row is not None:
            pages.append(
                {
                    "url": pu.url,
                    "clicks": int(row["clicks"]),
                    "impressions": int(row["impressions"]),
                    "ctr": float(row["ctr"]),
                    "position": float(row["position"]),
                }
            )
        else:
            pages.append(
                {"url": pu.url, "clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
            )

    return {"pages": pages, "totals": totals_from_df(df)}
