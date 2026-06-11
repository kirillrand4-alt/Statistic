"""Feature 3: total clicks/impressions — site-wide, per-page, and subset."""
from __future__ import annotations

from app.providers.base import DateRange
from app.services.loaders import (
    agg_metrics,
    load_device_metrics_df,
    load_page_metrics_df,
    load_site_totals_df,
    project_page_id_map,
    totals_from_df,
)


def _multi(site_id) -> bool:
    return isinstance(site_id, (list, tuple, set)) and len(set(site_id)) > 1


def _collapse_max(df, keys: list[str]):
    """One row per `keys` group — the most complete (max impressions).

    Used when merging same-domain properties (e.g. GSC ``https://`` + ``sc-domain:``):
    a domain property already includes the prefix one, so taking the max per
    (URL, day) / per day avoids double-counting instead of summing.
    """
    if df.empty:
        return df
    return df.sort_values("impressions").groupby(keys, as_index=False).last()


def site_totals(db, site_id, dr: DateRange) -> dict:
    df = load_site_totals_df(db, site_id, dr)
    if _multi(site_id):
        df = _collapse_max(df, ["date"])
    return totals_from_df(df)


def site_daily(db, site_id, dr: DateRange) -> list[dict]:
    """Daily site totals, for time-series charts."""
    df = load_site_totals_df(db, site_id, dr)
    if _multi(site_id):
        df = _collapse_max(df, ["date"])
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


def per_page_totals(db, site_id, dr: DateRange, page_ids=None) -> list[dict]:
    df = load_page_metrics_df(db, site_id, dr, page_ids=page_ids)
    if _multi(site_id):
        df = _collapse_max(df, ["url", "date"])
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


def per_page_with_devices(db, site_id: int, dr: DateRange, page_ids=None) -> list[dict]:
    """Per-page totals plus desktop/mobile split (where device data exists)."""
    base = {
        p["url"]: dict(p, desktop_impr=0, desktop_clicks=0, mobile_impr=0, mobile_clicks=0)
        for p in per_page_totals(db, site_id, dr, page_ids=page_ids)
    }
    df = load_device_metrics_df(db, site_id, dr, page_ids=page_ids)
    if not df.empty:
        g = df.groupby(["url", "device"], as_index=False).agg(
            clicks=("clicks", "sum"), impressions=("impressions", "sum")
        )
        for r in g.itertuples():
            row = base.get(r.url)
            if row is not None and r.device in ("desktop", "mobile"):
                row[f"{r.device}_impr"] = int(r.impressions)
                row[f"{r.device}_clicks"] = int(r.clicks)
    out = list(base.values())
    for row in out:
        for dev in ("desktop", "mobile"):
            i = row[f"{dev}_impr"]
            row[f"{dev}_ctr"] = (row[f"{dev}_clicks"] / i) if i else 0.0
    out.sort(key=lambda r: r["clicks"], reverse=True)
    return out


def subset_totals(db, project, dr: DateRange) -> dict:
    page_map = project_page_id_map(db, project.site_id, project)
    df = load_page_metrics_df(db, project.site_id, dr, page_ids=list(page_map.values()))
    return totals_from_df(df)
