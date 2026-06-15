"""All-keywords aggregation across pages/properties."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.bootstrap import ensure_sources
from app.db.models import Page, Query, QueryMetricDaily, Site
from app.providers.base import DateRange
from app.services import keywords as K
from app.utils import normalize_url, query_hash

DR = DateRange(date(2026, 6, 1), date(2026, 6, 30))


def _site(db, uri, code="gsc"):
    src = ensure_sources(db)[code]
    s = Site(source_id=src.id, property_uri=uri, display_name=uri)
    db.add(s)
    db.commit()
    return s


def _page(db, site, url):
    nu = normalize_url(url)
    pg = db.execute(
        select(Page).where(Page.site_id == site.id, Page.normalized_url == nu)
    ).scalar_one_or_none()
    if pg is None:
        pg = Page(site_id=site.id, url=url, normalized_url=nu)
        db.add(pg)
        db.flush()
    return pg.id


def _query(db, site, text):
    q = db.execute(
        select(Query).where(Query.site_id == site.id, Query.text_hash == query_hash(text))
    ).scalar_one_or_none()
    if q is None:
        q = Query(site_id=site.id, text=text, text_hash=query_hash(text))
        db.add(q)
        db.flush()
    return q.id


def _row(db, site, url, text, d, clicks, impr, pos):
    db.add(QueryMetricDaily(site_id=site.id, page_id=_page(db, site, url),
                            query_id=_query(db, site, text), date=d,
                            clicks=clicks, impressions=impr, position=pos))


def test_keyword_rows_aggregate_sum_and_filters(db):
    s = _site(db, "https://a.ru/")
    # same keyword on two pages + two days -> summed into one row
    _row(db, s, "https://a.ru/p1", "компрессор", date(2026, 6, 1), 5, 100, 3.0)
    _row(db, s, "https://a.ru/p2", "компрессор", date(2026, 6, 2), 3, 50, 5.0)
    _row(db, s, "https://a.ru/p1", "винтовой компрессор", date(2026, 6, 1), 1, 10, 8.0)
    _row(db, s, "https://a.ru/p3", "насос", date(2026, 6, 1), 0, 20, 12.0)
    db.commit()

    rows = K.keyword_rows(db, [s.id], DR)
    by = {r["query"]: r for r in rows}
    assert by["компрессор"]["clicks"] == 8 and by["компрессор"]["impressions"] == 150
    assert round(by["компрессор"]["position"], 2) == round((3 * 100 + 5 * 50) / 150, 2)
    assert rows[0]["query"] == "компрессор"  # sorted by clicks desc
    assert len(rows) == 3                     # 3 distinct keywords

    assert {r["query"] for r in K.keyword_rows(db, [s.id], DR, min_clicks=1)} == {
        "компрессор", "винтовой компрессор"}            # "насос" (0 clicks) dropped
    assert [r["query"] for r in K.keyword_rows(db, [s.id], DR, search="винтов")] == [
        "винтовой компрессор"]

    summ = K.keyword_summary(db, [s.id], DR)
    assert summ["keywords"] == 3 and summ["clicks"] == 9 and summ["impressions"] == 180


def test_keyword_rows_merge_across_properties(db):
    """Same keyword under two properties of one domain merges by text."""
    a = _site(db, "https://shop.ru/")
    b = _site(db, "sc-domain:shop.ru")
    _row(db, a, "https://shop.ru/x", "цена", date(2026, 6, 1), 4, 40, 2.0)
    _row(db, b, "https://shop.ru/x", "цена", date(2026, 6, 1), 2, 20, 6.0)
    db.commit()
    rows = K.keyword_rows(db, [a.id, b.id], DR)
    assert len(rows) == 1 and rows[0]["query"] == "цена"
    assert rows[0]["clicks"] == 6 and rows[0]["impressions"] == 60


def test_build_keyword_export_csv_has_all_rows(db):
    s = _site(db, "https://a.ru/")
    _row(db, s, "https://a.ru/p", "k1", date(2026, 6, 1), 2, 10, 1.0)
    _row(db, s, "https://a.ru/p", "k2", date(2026, 6, 1), 1, 5, 2.0)
    db.commit()
    rows = K.keyword_rows(db, [s.id], DR)
    name, buf, media = K.build_keyword_export(rows, "a.ru", DR, "csv")
    body = buf.getvalue().decode("utf-8-sig")
    assert name.endswith(".csv") and "Запрос" in body and "k1" in body and "k2" in body
