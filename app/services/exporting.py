"""Shared CSV/XLSX download builder used by every export.

Collapses the repeated ``DataFrame -> BytesIO`` boilerplate (CSV as UTF-8 with
BOM so Excel opens it cleanly; XLSX via openpyxl) into one place.
"""
from __future__ import annotations

import io

import pandas as pd

CSV_MEDIA = "text/csv"
XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def to_download(df: pd.DataFrame, name: str, fmt: str = "csv", *, sheet: str = "Данные"):
    """``(filename, BytesIO, media_type)`` for a single-sheet export.

    ``name`` is the base filename without extension; ``fmt`` is ``csv`` or ``xlsx``.
    """
    buf = io.BytesIO()
    if fmt == "csv":
        buf.write(df.to_csv(index=False).encode("utf-8-sig"))
        buf.seek(0)
        return f"{name}.csv", buf, CSV_MEDIA
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, sheet_name=sheet[:31], index=False)
    buf.seek(0)
    return f"{name}.xlsx", buf, XLSX_MEDIA
