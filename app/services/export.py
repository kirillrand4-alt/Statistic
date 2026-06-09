"""Feature 4: export detailed statistics for a date range (CSV / XLSX)."""
from __future__ import annotations

import io

import pandas as pd

from app.providers.base import DateRange
from app.services.loaders import (
    agg_metrics,
    load_page_metrics_df,
    load_query_metrics_df,
    load_site_totals_df,
    project_page_id_map,
)

CSV_MEDIA = "text/csv"
XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_RENAME = {
    "url": "URL",
    "query": "Запрос",
    "date": "Дата",
    "clicks": "Клики",
    "impressions": "Показы",
    "ctr": "CTR %",
    "position": "Позиция",
}


def _prettify(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.rename(columns=_RENAME)
    df = df.copy()
    if "ctr" in df:
        df["ctr"] = (df["ctr"] * 100).round(2)
    if "position" in df:
        df["position"] = df["position"].round(2)
    return df.rename(columns=_RENAME)


def _frames(db, site, dr: DateRange, project=None) -> dict[str, pd.DataFrame]:
    page_ids = None
    if project is not None:
        page_ids = list(project_page_id_map(db, site.id, project).values())

    totals = load_site_totals_df(db, site.id, dr)
    if not totals.empty:
        totals = totals.sort_values("date")
        totals["ctr"] = (totals["clicks"] / totals["impressions"].replace(0, pd.NA)).fillna(0.0)
        totals = totals[["date", "clicks", "impressions", "ctr", "position"]]

    per_page = agg_metrics(load_page_metrics_df(db, site.id, dr, page_ids=page_ids), ["url"])
    if not per_page.empty:
        per_page = per_page.sort_values("clicks", ascending=False)

    per_query = agg_metrics(
        load_query_metrics_df(db, site.id, dr, page_ids=page_ids), ["url", "query"]
    )
    if not per_query.empty:
        per_query = per_query.sort_values("clicks", ascending=False)

    return {"Сводка по сайту": totals, "По страницам": per_page, "По запросам": per_query}


def _filename(site, dr: DateRange, ext: str) -> str:
    # ASCII-only: Content-Disposition header values must be latin-1 encodable.
    label = site.display_name or site.property_uri
    safe = "".join(c if (c.isascii() and c.isalnum()) else "_" for c in label)[:40].strip("_")
    return f"seo_{safe or 'site'}_{dr.start.isoformat()}_{dr.end.isoformat()}.{ext}"


def build_export(db, site, dr: DateRange, level: str = "page", project=None, fmt: str = "xlsx"):
    """Return (filename, BytesIO, media_type)."""
    frames = _frames(db, site, dr, project=project)

    if fmt == "csv":
        level_map = {"totals": "Сводка по сайту", "page": "По страницам", "query": "По запросам"}
        df = _prettify(frames.get(level_map.get(level, "По страницам"), pd.DataFrame()))
        buf = io.BytesIO()
        buf.write(df.to_csv(index=False).encode("utf-8-sig"))
        buf.seek(0)
        return _filename(site, dr, "csv"), buf, CSV_MEDIA

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, df in frames.items():
            _prettify(df).to_excel(writer, sheet_name=name[:31], index=False)
    buf.seek(0)
    return _filename(site, dr, "xlsx"), buf, XLSX_MEDIA
