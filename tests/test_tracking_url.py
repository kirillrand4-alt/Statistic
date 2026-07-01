"""is_tracking_url: detect ad/tracking-tagged URLs that pollute organic page stats."""
from __future__ import annotations

import datetime as dt

from app.bootstrap import ensure_sources
from app.db.base import SessionLocal
from app.db.models import Page, PageMetricDaily, Site
from app.providers.base import DateRange
from app.services import totals as T
from app.utils import is_tracking_url, normalize_url


def test_ad_and_tracking_urls_flagged():
    assert is_tracking_url(
        "https://berg-kompressor.ru/?utm_source=yandex-berg&utm_medium=cpc"
        "&utm_content={gbid}|{ad_id}|{device_type}&roistat=direct10_{source_type}_709629496")
    assert is_tracking_url("https://berg-kompressor.ru/catalog?roistat=direct10_{PHRASE}")
    assert is_tracking_url("https://x.ru/?utm_campaign=sale")
    assert is_tracking_url("https://x.ru/?yclid=12345")
    assert is_tracking_url("https://x.ru/?gclid=abc")
    assert is_tracking_url("https://x.ru/land/{PHRASE}/")  # unfilled macro in path


def test_clean_and_benign_query_urls_not_flagged():
    assert not is_tracking_url("https://berg-kompressor.ru/")
    assert not is_tracking_url("https://berg-kompressor.ru/catalog/osushiteli/")
    assert not is_tracking_url("https://x.ru/catalog?page=2")       # benign pagination
    assert not is_tracking_url("https://x.ru/search?q=компрессор")  # benign query
    assert not is_tracking_url("")
    assert not is_tracking_url(None)


def test_tagged_daily_sums_only_tagged_urls():
    db = SessionLocal()
    try:
        gsc = ensure_sources(db)["gsc"]
        s = Site(source_id=gsc.id, property_uri="https://berg-kompressor.ru/", display_name="b")
        db.add(s)
        db.commit()

        def page(url, day, clicks):
            p = Page(site_id=s.id, url=url, normalized_url=normalize_url(url))
            db.add(p)
            db.commit()
            db.add(PageMetricDaily(site_id=s.id, page_id=p.id, date=dt.date(2026, 6, day),
                                   clicks=clicks, impressions=clicks * 10, ctr=0.1, position=6.0))
            db.commit()

        page("https://berg-kompressor.ru/?utm_source=y&roistat=direct10_{PHRASE}", 20, 149)
        page("https://berg-kompressor.ru/land2?utm_medium=cpc", 21, 50)
        page("https://berg-kompressor.ru/", 20, 113)              # clean — excluded
        page("https://berg-kompressor.ru/catalog/osushiteli/", 20, 37)  # clean — excluded

        dr = DateRange(start=dt.date(2026, 6, 1), end=dt.date(2026, 6, 30))
        td = {t["date"]: t["clicks"] for t in T.tagged_daily(db, s.id, dr)}
        assert td == {"2026-06-20": 149, "2026-06-21": 50}  # only tagged URLs, per day
    finally:
        db.close()
