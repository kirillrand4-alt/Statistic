"""Feature 5: period-over-period growth / decline.

Parameters: metric (clicks|impressions|ctr|position), period A (current) vs
period B (comparison), grouping (site|subset|page|query), optional page subset
and a ``min_impressions`` noise filter. For ``position`` lower is better, so the
``improved`` flag and sort direction are inverted.
"""
from __future__ import annotations

import pandas as pd

from app.providers.base import DateRange
from app.services.loaders import (
    agg_metrics,
    load_page_metrics_df,
    load_query_metrics_df,
    load_site_totals_df,
)

METRICS = ("clicks", "impressions", "ctr", "position")
GROUPINGS = ("site", "subset", "page", "query")


def _grouped_frame(db, site_id, dr: DateRange, grouping: str, page_ids) -> pd.DataFrame:
    if grouping == "site":
        df = load_site_totals_df(db, site_id, dr).assign(key="Весь сайт")
        return agg_metrics(df, ["key"])
    if grouping == "subset":
        df = load_page_metrics_df(db, site_id, dr, page_ids=page_ids).assign(key="Подмножество")
        return agg_metrics(df, ["key"])
    if grouping == "page":
        df = load_page_metrics_df(db, site_id, dr, page_ids=page_ids)
        return agg_metrics(df, ["url"]).rename(columns={"url": "key"})
    if grouping == "query":
        df = load_query_metrics_df(db, site_id, dr, page_ids=page_ids)
        return agg_metrics(df, ["query"]).rename(columns={"query": "key"})
    raise ValueError(f"Unknown grouping: {grouping!r}")


def _weighted(values, weights) -> float:
    w = float(weights.sum())
    return float((values * weights).sum() / w) if w else 0.0


def compare(
    db,
    site_id: int,
    metric: str = "clicks",
    *,
    period_a: DateRange,
    period_b: DateRange,
    grouping: str = "site",
    page_ids=None,
    min_impressions: int = 0,
) -> dict:
    metric = metric if metric in METRICS else "clicks"
    grouping = grouping if grouping in GROUPINGS else "site"

    ga = _grouped_frame(db, site_id, period_a, grouping, page_ids)
    gb = _grouped_frame(db, site_id, period_b, grouping, page_ids)
    merged = ga.merge(gb, on="key", how="outer", suffixes=("_a", "_b"))
    numeric = [f"{m}_{s}" for m in METRICS for s in ("a", "b")]
    for c in numeric:
        if c in merged:
            merged[c] = merged[c].fillna(0.0)

    rows = []
    for _, r in merged.iterrows():
        a, b = float(r[f"{metric}_a"]), float(r[f"{metric}_b"])
        imp_max = max(float(r["impressions_a"]), float(r["impressions_b"]))
        if min_impressions and imp_max < min_impressions:
            continue
        if metric == "position":
            improved = a > 0 and b > 0 and a < b
        else:
            improved = a > b
        rows.append(
            {
                "key": r["key"],
                "value_a": a,
                "value_b": b,
                "delta": a - b,
                "pct_change": ((a - b) / b * 100.0) if b else None,
                "improved": bool(improved),
                "impressions_a": int(r["impressions_a"]),
                "impressions_b": int(r["impressions_b"]),
            }
        )

    rows.sort(key=lambda x: x["delta"], reverse=(metric != "position"))

    summary = _summary(merged, metric)
    return {"metric": metric, "grouping": grouping, "rows": rows, "summary": summary}


def _summary(merged: pd.DataFrame, metric: str) -> dict:
    if merged.empty:
        return {"metric": metric, "value_a": 0.0, "value_b": 0.0, "delta": 0.0, "pct_change": None}

    def agg_side(s: str) -> dict:
        clicks = float(merged[f"clicks_{s}"].sum())
        impr = float(merged[f"impressions_{s}"].sum())
        return {
            "clicks": clicks,
            "impressions": impr,
            "ctr": (clicks / impr) if impr else 0.0,
            "position": _weighted(merged[f"position_{s}"], merged[f"impressions_{s}"]),
        }

    a, b = agg_side("a"), agg_side("b")
    va, vb = a[metric], b[metric]
    return {
        "metric": metric,
        "value_a": va,
        "value_b": vb,
        "delta": va - vb,
        "pct_change": ((va - vb) / vb * 100.0) if vb else None,
    }
