"""Feature 3: total clicks/impressions — site-wide, per-page, and subset."""
from __future__ import annotations

from datetime import date

from app.providers.base import DateRange
from app.services.loaders import (
    agg_metrics,
    load_device_metrics_df,
    load_page_metrics_df,
    load_site_totals_df,
    project_page_ids,
    totals_from_df,
)


def bucket_label(d: date, gran: str) -> str:
    """Chronologically-sortable bucket label for a date: the day itself, the
    week's Monday, or YYYY-MM."""
    if gran == "week":
        y, w, _ = d.isocalendar()
        return date.fromisocalendar(y, w, 1).isoformat()
    if gran == "month":
        return f"{d.year:04d}-{d.month:02d}"
    return d.isoformat()


def bucket_series(daily: list[dict], gran: str, agg: str = "sum") -> list[dict]:
    """Roll a daily series up to week/month. ``agg='sum'`` for flows (clicks,
    impressions, event counts) — ctr/position are recomputed (impression-
    weighted); ``agg='last'`` for stocks (e.g. pages-in-index) keeps the last
    snapshot of each bucket. ``gran='day'`` (or empty input) returns it as-is."""
    if gran not in ("week", "month") or not daily:
        return daily
    order: list[str] = []
    buckets: dict[str, dict] = {}
    for r in daily:
        label = bucket_label(date.fromisoformat(r["date"]), gran)
        if label not in buckets:
            buckets[label] = {"date": label, "_pw": 0.0} if agg == "sum" else None
            order.append(label)
        if agg == "last":  # later rows win (input is date-ascending)
            buckets[label] = {**r, "date": label}
            continue
        b = buckets[label]
        for f, v in r.items():
            if f != "date" and f not in ("ctr", "position"):
                b[f] = b.get(f, 0) + v
        if "position" in r and "impressions" in r:
            b["_pw"] += r["position"] * r["impressions"]
    out = []
    for label in order:
        b = buckets[label]
        if agg == "sum":
            pw = b.pop("_pw", 0.0)
            if "clicks" in b and "impressions" in b:
                im = b["impressions"]
                b["ctr"] = (b["clicks"] / im) if im else 0.0
                b["position"] = (pw / im) if im else 0.0
        out.append(b)
    return out


def site_totals(db, site_id, dr: DateRange) -> dict:
    # Overlap of merged same-domain properties is de-duplicated inside the loaders.
    return totals_from_df(load_site_totals_df(db, site_id, dr))


def site_daily(db, site_id, dr: DateRange) -> list[dict]:
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


def tagged_daily(db, site_id, dr: DateRange) -> list[dict]:
    """Daily clicks/impressions from ad/tracking-tagged URLs (utm_/roistat/{macros})
    that leaked into the organic index — for a "how much is tagged" line on the chart."""
    from app.utils import is_tracking_url
    df = load_page_metrics_df(db, site_id, dr)
    if df.empty:
        return []
    df = df[df["url"].map(is_tracking_url)]
    if df.empty:
        return []
    g = df.groupby("date", as_index=False).agg(
        clicks=("clicks", "sum"), impressions=("impressions", "sum"))
    out = []
    for _, r in g.iterrows():
        imp = int(r["impressions"])
        d = r["date"]
        out.append({"date": d.isoformat() if hasattr(d, "isoformat") else str(d),
                    "clicks": int(r["clicks"]), "impressions": imp,
                    "ctr": (int(r["clicks"]) / imp) if imp else 0.0, "position": 0.0})
    return out


def per_page_totals(db, site_id, dr: DateRange, page_ids=None) -> list[dict]:
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


# --- combine results across engines (Google + Yandex) ------------------------
# Each `part` is the result for ONE engine (its same-domain https/sc-domain
# properties already de-duplicated inside the loaders, max-on-overlap). Across
# engines the data is independent, so we SUM; position is impression-weighted.

def _derive(clicks: int, impressions: int, pos_weight: float) -> dict:
    return {"clicks": clicks, "impressions": impressions,
            "ctr": (clicks / impressions) if impressions else 0.0,
            "position": (pos_weight / impressions) if impressions else 0.0}


def combine_totals(parts: list[dict]) -> dict:
    cl = sum(p["clicks"] for p in parts)
    im = sum(p["impressions"] for p in parts)
    pw = sum(p["position"] * p["impressions"] for p in parts)
    return _derive(cl, im, pw)


def _combine_rows(parts, key: str) -> dict:
    acc: dict = {}
    for rows in parts:
        for r in rows:
            b = acc.setdefault(r[key], {"clicks": 0, "impressions": 0, "pw": 0.0, "extra": r})
            b["clicks"] += r["clicks"]
            b["impressions"] += r["impressions"]
            b["pw"] += r["position"] * r["impressions"]
    return acc


def combine_daily(parts: list[list[dict]]) -> list[dict]:
    acc = _combine_rows(parts, "date")
    return [dict(date=d, **_derive(b["clicks"], b["impressions"], b["pw"]))
            for d, b in sorted(acc.items())]


def combine_pages(parts: list[list[dict]], limit: int = 20) -> list[dict]:
    acc = _combine_rows(parts, "url")
    out = [dict(url=u, **_derive(b["clicks"], b["impressions"], b["pw"]))
           for u, b in acc.items()]
    out.sort(key=lambda r: r["clicks"], reverse=True)
    return out[:limit]


def combine_pages_devices(parts: list[list[dict]], limit: int = 20) -> list[dict]:
    """Combine per-page rows that also carry desktop_/mobile_ splits."""
    add = ("clicks", "impressions", "desktop_impr", "desktop_clicks",
           "mobile_impr", "mobile_clicks")
    acc: dict = {}
    for rows in parts:
        for r in rows:
            b = acc.setdefault(r["url"], {k: 0 for k in add} | {"pw": 0.0})
            for k in add:
                b[k] += r.get(k, 0)
            b["pw"] += r["position"] * r["impressions"]
    out = []
    for url, b in acc.items():
        im = b["impressions"]
        row = {"url": url, "clicks": b["clicks"], "impressions": im,
               "ctr": (b["clicks"] / im) if im else 0.0,
               "position": (b["pw"] / im) if im else 0.0}
        for dev in ("desktop", "mobile"):
            di = b[f"{dev}_impr"]
            row[f"{dev}_impr"] = di
            row[f"{dev}_clicks"] = b[f"{dev}_clicks"]
            row[f"{dev}_ctr"] = (b[f"{dev}_clicks"] / di) if di else 0.0
        out.append(row)
    out.sort(key=lambda r: r["clicks"], reverse=True)
    return out[:limit]


def subset_totals(db, project, dr: DateRange, site_ids=None, only_norms=None) -> dict:
    ids = site_ids or project.site_id
    page_ids = project_page_ids(db, ids, project, only_norms=only_norms)
    df = load_page_metrics_df(db, ids, dr, page_ids=page_ids)
    return totals_from_df(df)


def subset_daily(db, project, dr: DateRange, site_ids=None, only_norms=None) -> list[dict]:
    """Daily totals for the project's URL subset (for a time-series chart)."""
    ids = site_ids or project.site_id
    page_ids = project_page_ids(db, ids, project, only_norms=only_norms)
    agg = agg_metrics(load_page_metrics_df(db, ids, dr, page_ids=page_ids), ["date"])
    if agg.empty:
        return []
    agg = agg.sort_values("date")
    return [
        {"date": d.isoformat() if hasattr(d, "isoformat") else str(d),
         "clicks": int(r["clicks"]), "impressions": int(r["impressions"]),
         "ctr": float(r["ctr"]), "position": float(r["position"])}
        for d, r in zip(agg["date"], agg.to_dict("records"))
    ]
