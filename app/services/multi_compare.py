"""Compare two periods for a project's URL list across BOTH engines at once.

The project's domain is matched against sites of every source (Google Search
Console, Yandex Webmaster); for each URL the chosen metric is aggregated over
period A and period B per engine, with delta / % change. Powers the
"Google + Яндекс" project comparison page and its CSV/XLSX export.
"""
from __future__ import annotations

import io

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Site, Source
from app.providers.base import DateRange
from app.services.export import CSV_MEDIA, XLSX_MEDIA
from app.services.loaders import (
    agg_metrics,
    load_device_metrics_df,
    load_page_metrics_df,
    project_page_id_map,
)
from app.utils import domain_of, normalize_url

ENGINES = ("gsc", "yandex_webmaster")
ENGINE_LABELS = {"gsc": "Google", "yandex_webmaster": "Яндекс"}
METRICS = ("clicks", "impressions", "ctr", "position")


def sites_for_project(db: Session, project) -> dict[str, Site]:
    """One site per source sharing the project's domain (own site wins)."""
    own = db.get(Site, project.site_id)
    domain = domain_of(own.property_uri)
    out: dict[str, Site] = {}
    sites = (
        db.execute(select(Site).join(Source, Site.source_id == Source.id).where(Source.code.in_(ENGINES)))
        .scalars()
        .all()
    )
    for s in sites:
        if domain_of(s.property_uri) != domain:
            continue
        code = s.source.code
        if s.id == project.site_id or code not in out:
            out[code] = s
    return out


def _values_by_url(db, site_id: int, project, dr: DateRange, exclude_bots: bool = False,
                   ratio: float = 10.0, min_impr: int = 100, device: str = "all") -> dict[str, dict]:
    page_map = project_page_id_map(db, site_id, project)
    page_ids = list(page_map.values())
    if device in ("desktop", "mobile"):
        df = load_device_metrics_df(db, site_id, dr, page_ids=page_ids)
        if not df.empty:
            df = df[df["device"] == device]
        out: dict[str, dict] = {}
        if not df.empty:
            for _, r in agg_metrics(df, ["url"]).iterrows():
                out[normalize_url(r["url"])] = {
                    "clicks": float(r["clicks"]), "impressions": float(r["impressions"]),
                    "ctr": float(r["ctr"]), "position": float(r["position"]),
                }
        return out
    if exclude_bots:
        from app.services.antifraud import clean_values_by_url

        return clean_values_by_url(db, site_id, dr, page_ids=page_ids,
                                   ratio_threshold=ratio, min_impressions=min_impr)
    df = load_page_metrics_df(db, site_id, dr, page_ids=page_ids)
    out: dict[str, dict] = {}
    if df.empty:
        return out
    for _, r in agg_metrics(df, ["url"]).iterrows():
        out[normalize_url(r["url"])] = {
            "clicks": float(r["clicks"]),
            "impressions": float(r["impressions"]),
            "ctr": float(r["ctr"]),
            "position": float(r["position"]),
        }
    return out


def _totals(vals: dict[str, dict]) -> dict:
    clicks = sum(v["clicks"] for v in vals.values())
    impr = sum(v["impressions"] for v in vals.values())
    pos_w = sum(v["position"] * v["impressions"] for v in vals.values())
    return {
        "clicks": clicks,
        "impressions": impr,
        "ctr": (clicks / impr) if impr else 0.0,
        "position": (pos_w / impr) if impr else 0.0,
    }


def _diff(metric: str, a: float, b: float) -> dict:
    if metric == "position":
        improved = a > 0 and b > 0 and a < b
    else:
        improved = a > b
    return {
        "a": a,
        "b": b,
        "delta": a - b,
        "pct": ((a - b) / b * 100.0) if b else None,
        "improved": bool(improved),
    }


def compare_project(db, project, metric: str, period_a: DateRange, period_b: DateRange,
                    exclude_bots: bool = False, ratio: float = 10.0, min_impr: int = 100,
                    device: str = "all") -> dict:
    metric = metric if metric in METRICS else "clicks"
    device = device if device in ("all", "desktop", "mobile") else "all"
    sites = sites_for_project(db, project)

    engines: dict[str, dict] = {}
    per_url: dict[str, tuple[dict, dict]] = {}
    for code, site in sites.items():
        a_vals = _values_by_url(db, site.id, project, period_a, exclude_bots, ratio, min_impr, device)
        b_vals = _values_by_url(db, site.id, project, period_b, exclude_bots, ratio, min_impr, device)
        per_url[code] = (a_vals, b_vals)
        tot_a, tot_b = _totals(a_vals), _totals(b_vals)
        engines[code] = {
            **_diff(metric, tot_a[metric], tot_b[metric]),
            "totals_a": tot_a,
            "totals_b": tot_b,
        }

    rows = []
    for pu in project.urls:
        row_engines = {}
        for code in per_url:
            a_vals, b_vals = per_url[code]
            va = a_vals.get(pu.normalized_url, {}).get(metric, 0.0)
            vb = b_vals.get(pu.normalized_url, {}).get(metric, 0.0)
            row_engines[code] = _diff(metric, va, vb)
        rows.append({"url": pu.url, "engines": row_engines})

    return {
        "metric": metric,
        "exclude_bots": exclude_bots,
        "device": device,
        "period_a": {"start": period_a.start.isoformat(), "end": period_a.end.isoformat()},
        "period_b": {"start": period_b.start.isoformat(), "end": period_b.end.isoformat()},
        "engines": engines,
        "rows": rows,
    }


def build_compare_export(result: dict, project, fmt: str = "xlsx"):
    """Return (filename, BytesIO, media_type) for the two-engine comparison."""
    metric = result["metric"]

    def disp(v):
        if v is None:
            return None
        if metric == "ctr":
            return round(v * 100, 2)
        if metric == "position":
            return round(v, 2)
        return int(round(v))

    out_rows = []
    for r in result["rows"]:
        out = {"URL": r["url"]}
        for code in ENGINES:
            e = r["engines"].get(code)
            lb = ENGINE_LABELS[code]
            out[f"{lb}: период A"] = disp(e["a"]) if e else None
            out[f"{lb}: период B"] = disp(e["b"]) if e else None
            out[f"{lb}: изменение"] = disp(e["delta"]) if e else None
            out[f"{lb}: %"] = round(e["pct"], 1) if (e and e["pct"] is not None) else None
        out_rows.append(out)
    df = pd.DataFrame(out_rows)

    safe = "".join(c if (c.isascii() and c.isalnum()) else "_" for c in project.name)[:30].strip("_")
    a, b = result["period_a"], result["period_b"]
    dev = result.get("device", "all")
    suffix = f"_{dev}" if dev != "all" else ""
    base = f"compare2_{safe or 'project'}_{metric}{suffix}_{a['start']}_{a['end']}_vs_{b['start']}_{b['end']}"

    buf = io.BytesIO()
    if fmt == "csv":
        buf.write(df.to_csv(index=False).encode("utf-8-sig"))
        buf.seek(0)
        return f"{base}.csv", buf, CSV_MEDIA
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Google vs Яндекс"[:31], index=False)
    buf.seek(0)
    return f"{base}.xlsx", buf, XLSX_MEDIA
