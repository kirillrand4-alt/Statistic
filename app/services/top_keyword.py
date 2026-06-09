"""Feature 1: the TOP-1 keyword for each URL in a project."""
from __future__ import annotations

from app.providers.base import DateRange
from app.services.loaders import agg_metrics, load_query_metrics_df, project_page_id_map
from app.utils import normalize_url

_SORT = {
    "clicks": (["clicks", "impressions"], [False, False]),
    "impressions": (["impressions", "clicks"], [False, False]),
    "position": (["position", "clicks"], [True, False]),  # lower position is better
}


def top_keywords_for_project(db, project, dr: DateRange, order_by: str = "clicks") -> list[dict]:
    if order_by not in _SORT:
        order_by = "clicks"
    page_map = project_page_id_map(db, project.site_id, project)
    df = load_query_metrics_df(db, project.site_id, dr, page_ids=list(page_map.values()))

    best_by_norm: dict[str, dict] = {}
    if not df.empty:
        agg = agg_metrics(df, ["url", "query"])
        agg["_norm"] = agg["url"].map(normalize_url)
        cols, asc = _SORT[order_by]
        for norm, group in agg.groupby("_norm"):
            row = group.sort_values(cols, ascending=asc).iloc[0]
            best_by_norm[norm] = row

    results = []
    for pu in project.urls:
        row = best_by_norm.get(pu.normalized_url)
        if row is not None:
            results.append(
                {
                    "url": pu.url,
                    "top_query": row["query"],
                    "clicks": int(row["clicks"]),
                    "impressions": int(row["impressions"]),
                    "ctr": float(row["ctr"]),
                    "position": float(row["position"]),
                }
            )
        else:
            results.append(
                {
                    "url": pu.url,
                    "top_query": None,
                    "clicks": 0,
                    "impressions": 0,
                    "ctr": 0.0,
                    "position": 0.0,
                }
            )
    return results
