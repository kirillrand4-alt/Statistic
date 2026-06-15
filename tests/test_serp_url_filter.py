"""URL list -> keywords from base -> filter stored SERP (no arsenkin re-run)."""
from __future__ import annotations

from datetime import date

from app.db.models import SerpResult
from app.providers.base import DateRange, QueryMetricRow
from app.services import keywords as KW
from app.services import serp as S
from app.services.ingest import upsert_query_metrics

DR = DateRange(start=date(2026, 5, 1), end=date(2026, 6, 30))
H = "https://example.com"


def test_keywords_for_urls_from_base(db, site):
    upsert_query_metrics(db, site, [
        QueryMetricRow(query="ремонт", url=f"{H}/a", date=date(2026, 6, 1),
                       clicks=10, impressions=100, ctr=0.1, position=3),
        QueryMetricRow(query="цена", url=f"{H}/a", date=date(2026, 6, 1),
                       clicks=5, impressions=80, ctr=0.06, position=5),
        QueryMetricRow(query="доставка", url=f"{H}/b", date=date(2026, 6, 1),
                       clicks=8, impressions=90, ctr=0.08, position=4),
    ])
    db.commit()
    # keywords of /a only (matched http/https/www-agnostic)
    assert set(KW.keywords_for_urls(db, [site.id], ["http://example.com/a"], DR)) == {"ремонт", "цена"}
    # a URL we have no data for -> empty
    assert KW.keywords_for_urls(db, [site.id], ["https://other.ru/x"], DR) == []
    # min_clicks filter
    assert KW.keywords_for_urls(db, [site.id], [f"{H}/a"], DR, min_clicks=8) == ["ремонт"]


def test_serp_rows_keyword_and_top3_filter(db):
    db.add_all([
        SerpResult(keyword="ремонт", se=2, region=213, position=1, url="https://a.ru/",
                   url_domain="a.ru", captured_on=date(2026, 6, 1)),
        SerpResult(keyword="ремонт", se=2, region=213, position=4, url="https://b.ru/",
                   url_domain="b.ru", captured_on=date(2026, 6, 1)),
        SerpResult(keyword="ремонт", se=11, region=1, position=2, url="https://g.ru/",
                   url_domain="g.ru", captured_on=date(2026, 6, 1)),
        SerpResult(keyword="цена", se=2, region=213, position=2, url="https://c.ru/",
                   url_domain="c.ru", captured_on=date(2026, 6, 1)),
    ])
    db.commit()
    # filter to the URL's keywords (case-insensitive) + top-3 per engine
    rows = S.serp_rows(db, captured_on="2026-06-01", keywords={"Ремонт"}, max_position=3)
    assert {r["keyword"] for r in rows} == {"ремонт"}
    assert all(r["position"] <= 3 for r in rows)
    # both engines kept (se 2 #1 and se 11 #2); the se=2 #4 row is dropped
    assert sorted((r["se"], r["position"]) for r in rows) == [(2, 1), (11, 2)]
    # a keyword we don't have -> nothing
    assert S.serp_rows(db, captured_on="2026-06-01", keywords={"нет такого"}) == []
