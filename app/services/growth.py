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


_CLEAN_COLS = ["key", "clicks", "impressions", "ctr", "position"]


def _clean_frame(db, site_id, dr, grouping, page_ids, ratio, min_impr) -> pd.DataFrame:
    """Bot-filtered grouped frame (site/subset/page) from device-split data."""
    from app.services.antifraud import clean_values_by_url

    pids = page_ids if grouping in ("subset", "page") else None
    vals = clean_values_by_url(db, site_id, dr, page_ids=pids, ratio_threshold=ratio, min_impressions=min_impr)
    if grouping == "page":
        recs = [{"key": u, **{k: v[k] for k in ("clicks", "impressions", "ctr", "position")}} for u, v in vals.items()]
        return pd.DataFrame(recs, columns=_CLEAN_COLS)
    clicks = sum(v["clicks"] for v in vals.values())
    impr = sum(v["impressions"] for v in vals.values())
    posw = sum(v["position"] * v["impressions"] for v in vals.values())
    key = "Весь сайт" if grouping == "site" else "Подмножество"
    return pd.DataFrame(
        [{"key": key, "clicks": clicks, "impressions": impr,
          "ctr": (clicks / impr) if impr else 0.0, "position": (posw / impr) if impr else 0.0}],
        columns=_CLEAN_COLS,
    )


def _grouped_frame(db, site_id, dr: DateRange, grouping: str, page_ids,
                   exclude_bots: bool = False, ratio: float = 10.0, min_impr: int = 100) -> pd.DataFrame:
    if exclude_bots and grouping in ("site", "subset", "page"):
        return _clean_frame(db, site_id, dr, grouping, page_ids, ratio, min_impr)
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
    exclude_bots: bool = False,
    ratio: float = 10.0,
    min_impr: int = 100,
) -> dict:
    metric = metric if metric in METRICS else "clicks"
    grouping = grouping if grouping in GROUPINGS else "site"

    ga = _grouped_frame(db, site_id, period_a, grouping, page_ids, exclude_bots, ratio, min_impr)
    gb = _grouped_frame(db, site_id, period_b, grouping, page_ids, exclude_bots, ratio, min_impr)
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
    return {"metric": metric, "grouping": grouping, "exclude_bots": exclude_bots,
            "rows": rows, "summary": summary}


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
