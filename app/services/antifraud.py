"""Bot/fraud detection by desktop-vs-mobile imbalance.

If a page's impressions on one device are >= ``ratio_threshold`` times the
other, the inflated side is treated as bot traffic and subtracted to give
"clean" stats. Low-volume pages (<= ``min_impressions`` over the period) are
ignored as noise.
"""
from __future__ import annotations

import math

from app.providers.base import DateRange
from app.services.loaders import load_device_metrics_df


def _ctr(clicks: int, impr: int) -> float:
    return (clicks / impr) if impr else 0.0


def analyze(db, site_id: int, dr: DateRange, ratio_threshold: float = 10.0,
            min_impressions: int = 100) -> dict:
    df = load_device_metrics_df(db, site_id, dr)
    raw_impr = raw_clicks = clean_impr = clean_clicks = 0
    rows = []

    if not df.empty:
        g = df.groupby(["url", "device"], as_index=False).agg(
            clicks=("clicks", "sum"), impressions=("impressions", "sum")
        )
        for url, sub in g.groupby("url"):
            dev = {r.device: (int(r.impressions), int(r.clicks)) for r in sub.itertuples()}
            d_impr, d_clk = dev.get("desktop", (0, 0))
            m_impr, m_clk = dev.get("mobile", (0, 0))
            total_impr = sum(v[0] for v in dev.values())
            total_clk = sum(v[1] for v in dev.values())
            raw_impr += total_impr
            raw_clicks += total_clk

            hi, lo = max(d_impr, m_impr), min(d_impr, m_impr)
            ratio = (hi / lo) if lo > 0 else (math.inf if hi > 0 else 1.0)

            if total_impr > min_impressions and ratio >= ratio_threshold:
                bot = "desktop" if d_impr > m_impr else "mobile"
                bot_impr = d_impr if bot == "desktop" else m_impr
                bot_clk = d_clk if bot == "desktop" else m_clk
                c_impr, c_clk = total_impr - bot_impr, total_clk - bot_clk
                clean_impr += c_impr
                clean_clicks += c_clk
                rows.append(
                    {
                        "url": url,
                        "desktop_impr": d_impr, "desktop_ctr": _ctr(d_clk, d_impr),
                        "mobile_impr": m_impr, "mobile_ctr": _ctr(m_clk, m_impr),
                        "ratio": None if math.isinf(ratio) else round(ratio, 1),
                        "bot_device": bot,
                        "removed_impr": bot_impr,
                        "clean_impr": c_impr, "clean_clicks": c_clk,
                    }
                )
            else:
                clean_impr += total_impr
                clean_clicks += total_clk

    rows.sort(key=lambda r: r["removed_impr"], reverse=True)
    return {
        "rows": rows,
        "summary": {
            "flagged": len(rows),
            "raw_impressions": raw_impr,
            "raw_clicks": raw_clicks,
            "clean_impressions": clean_impr,
            "clean_clicks": clean_clicks,
            "removed_impressions": raw_impr - clean_impr,
            "ratio_threshold": ratio_threshold,
            "min_impressions": min_impressions,
        },
    }
