"""Shared helpers extracted from duplicated code: as_id_list, to_download,
resolve_page_ids (the last also covers the /pages http↔https matching fix)."""
from __future__ import annotations

import pandas as pd

from app.services.exporting import CSV_MEDIA, XLSX_MEDIA, to_download
from app.services.ingest import ensure_pages
from app.services.loaders import resolve_page_ids
from app.utils import as_id_list


def test_as_id_list():
    assert as_id_list(5) == [5]
    assert as_id_list([1, 2]) == [1, 2]
    assert as_id_list((1, 2)) == [1, 2]
    assert as_id_list("abc") == ["abc"]          # str treated as one value, not chars


def test_to_download_csv_and_xlsx():
    df = pd.DataFrame([{"A": 1, "Б": "тест"}])
    fn, buf, media = to_download(df, "rep", "csv")
    assert fn == "rep.csv" and media == CSV_MEDIA
    body = buf.getvalue().decode("utf-8-sig")
    assert "A" in body and "тест" in body
    fn2, buf2, media2 = to_download(df, "rep", "xlsx", sheet="Лист")
    assert fn2 == "rep.xlsx" and media2 == XLSX_MEDIA
    assert buf2.getvalue()[:2] == b"PK"          # .xlsx is a zip container


def test_resolve_page_ids_scheme_agnostic(db, site):
    ensure_pages(db, site, ["https://example.com/a", "https://example.com/b"])
    db.commit()
    # stored as https; uploaded as http with a trailing slash -> still matches
    ids = resolve_page_ids(db, [site.id], ["http://example.com/a/"])
    assert len(ids) == 1
    assert resolve_page_ids(db, [site.id], ["https://other.ru/x"]) == []
    assert resolve_page_ids(db, [], ["https://example.com/a"]) == []
