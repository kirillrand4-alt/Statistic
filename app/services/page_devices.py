"""Per-page metrics split by device (desktop / mobile) with period-over-period
change: clicks, impressions and CTR for each device, period A vs B, and whether
each page improved or declined.

Built on the device-split data we already collect (``device_metric_daily`` via
GSC ``page×device×date`` and the Yandex equivalent). Pick one engine at a time so
desktop/mobile aren't double-counted across properties.
"""
from __future__ import annotations

import pandas as pd

from app.providers.base import DateRange
from app.services.exporting import to_download
from app.services.loaders import agg_metrics, load_device_metrics_df

DEVICES = ("desktop", "mobile")


def _load(db, site_ids, dr: DateRange, page_ids):
    """({(url, device): (clicks, impr)}, {url: (clicks, impr)}) for a period."""
    df = load_device_metrics_df(db, site_ids, dr, page_ids=page_ids)
    cells, totals = {}, {}
    if df.empty:
        return cells, totals
    for _, r in agg_metrics(df, ["url", "device"]).iterrows():
        cells[(r["url"], str(r["device"]).lower())] = (int(r["clicks"]), int(r["impressions"]))
    for _, r in agg_metrics(df, ["url"]).iterrows():
        totals[r["url"]] = (int(r["clicks"]), int(r["impressions"]))
    return cells, totals


def _ctr(c, i):
    return (c / i) if i else 0.0


def _cell(a, b) -> dict:
    ca, ia = a
    cb, ib = b
    return {
        "clicks_a": ca, "clicks_b": cb, "clicks_delta": ca - cb,
        "impr_a": ia, "impr_b": ib, "impr_delta": ia - ib,
        "ctr_a": _ctr(ca, ia), "ctr_b": _ctr(cb, ib),
        "ctr_delta": _ctr(ca, ia) - _ctr(cb, ib),
    }


_SORTS = {
    "clicks": (lambda r: r["total"]["clicks_a"], True),
    "growth": (lambda r: r["total"]["clicks_delta"], True),   # biggest gainers
    "drop": (lambda r: r["total"]["clicks_delta"], False),    # biggest losers
    "impr": (lambda r: r["total"]["impr_a"], True),
}


def page_device_compare(db, site_ids, period_a: DateRange, period_b: DateRange, *,
                        page_ids=None, search: str | None = None,
                        min_impressions: int = 0, sort: str = "clicks",
                        devices=DEVICES, limit: int | None = 500) -> dict:
    """Per-URL desktop/mobile clicks·impr·CTR for A vs B, with deltas. ``improved``
    is by total clicks. Sorted per ``sort`` (clicks/growth/drop/impr)."""
    ca, ta = _load(db, site_ids, period_a, page_ids)
    cb, tb = _load(db, site_ids, period_b, page_ids)
    rows = []
    for url in set(ta) | set(tb):
        if search and search.lower() not in (url or "").lower():
            continue
        tot_a, tot_b = ta.get(url, (0, 0)), tb.get(url, (0, 0))
        if min_impressions and max(tot_a[1], tot_b[1]) < min_impressions:
            continue
        rows.append({
            "url": url,
            "dev": {d: _cell(ca.get((url, d), (0, 0)), cb.get((url, d), (0, 0))) for d in devices},
            "total": _cell(tot_a, tot_b),
            "improved": tot_a[0] > tot_b[0],
        })
    keyf, rev = _SORTS.get(sort, _SORTS["clicks"])
    rows.sort(key=keyf, reverse=rev)
    total = len(rows)
    return {"rows": rows[:limit] if limit else rows, "total": total, "devices": list(devices)}


def build_export(rows: list[dict], devices, label: str, fmt: str = "csv"):
    data = []
    for r in rows:
        rec = {"URL": r["url"]}
        for d in devices:
            c = r["dev"][d]
            dl = {"desktop": "Десктоп", "mobile": "Моб."}.get(d, d)
            rec[f"{dl} клики A"] = c["clicks_a"]
            rec[f"{dl} клики B"] = c["clicks_b"]
            rec[f"{dl} клики Δ"] = c["clicks_delta"]
            rec[f"{dl} показы A"] = c["impr_a"]
            rec[f"{dl} показы B"] = c["impr_b"]
            rec[f"{dl} CTR A %"] = round(c["ctr_a"] * 100, 2)
            rec[f"{dl} CTR B %"] = round(c["ctr_b"] * 100, 2)
        t = r["total"]
        rec["Итого клики A"] = t["clicks_a"]
        rec["Итого клики B"] = t["clicks_b"]
        rec["Итого клики Δ"] = t["clicks_delta"]
        rec["Итого показы A"] = t["impr_a"]
        rec["Итого показы B"] = t["impr_b"]
        rec["Итого CTR A %"] = round(t["ctr_a"] * 100, 2)
        rec["Итого CTR B %"] = round(t["ctr_b"] * 100, 2)
        data.append(rec)
    df = pd.DataFrame(data)
    safe = "".join(c if (c.isascii() and c.isalnum()) else "_" for c in (label or "all"))[:30]
    name = f"pages_devices_{safe.strip('_') or 'all'}"
    return to_download(df, name, fmt, sheet="Страницы устройства")
