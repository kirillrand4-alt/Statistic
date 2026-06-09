"""Export service: produces valid CSV and multi-sheet XLSX."""
from __future__ import annotations

import io
from datetime import date, timedelta

import pandas as pd

from app.providers.base import DateRange
from app.scheduler.jobs import collect_site
from app.services.export import build_export


def _range():
    end = date.today() - timedelta(days=1)
    return DateRange(start=end - timedelta(days=14), end=end)


def test_export_csv(db, site, project):
    dr = _range()
    collect_site(db, site, dr)
    site.display_name = "example.com (демо)"  # Cyrillic must not break the HTTP header
    filename, buf, media = build_export(db, site, dr, level="page", project=project, fmt="csv")
    assert filename.endswith(".csv")
    assert filename.isascii(), "Content-Disposition filename must be latin-1/ASCII safe"
    assert media == "text/csv"
    df = pd.read_csv(io.BytesIO(buf.getvalue()))
    assert "URL" in df.columns and "Клики" in df.columns
    assert len(df) > 0


def test_export_xlsx_sheets(db, site, project):
    dr = _range()
    collect_site(db, site, dr)
    filename, buf, media = build_export(db, site, dr, fmt="xlsx")
    assert filename.endswith(".xlsx")
    xls = pd.ExcelFile(io.BytesIO(buf.getvalue()))
    assert "По страницам" in xls.sheet_names
    assert "По запросам" in xls.sheet_names
