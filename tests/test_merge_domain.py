"""Merging same-domain properties (e.g. GSC https:// + sc-domain:) without
double-counting the overlap."""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import ensure_sources
from app.db.models import Page, PageMetricDaily, Site, SiteTotalDaily
from app.providers import register_override
from app.providers.base import DateRange
from app.providers.mock import MockProvider
from app.services import totals as T


@pytest.fixture()
def client():
    register_override("gsc", MockProvider())
    register_override("yandex_webmaster", MockProvider())
    from app.main import app

    with TestClient(app) as c:
        yield c


def _site(db, uri):
    gsc = ensure_sources(db)["gsc"]
    s = Site(source_id=gsc.id, property_uri=uri, display_name=uri)
    db.add(s)
    db.commit()
    return s


def _page(db, site_id, url):
    p = Page(site_id=site_id, url=url, normalized_url=url)
    db.add(p)
    db.commit()
    return p.id


def test_merge_dedups_overlapping_properties(db):
    a = _site(db, "sc-domain:example.com")     # domain property
    b = _site(db, "https://example.com/")       # url-prefix property (overlaps a)
    d = date(2026, 6, 1)
    # same URL on the same day in BOTH properties -> must take MAX, not SUM
    pa = _page(db, a.id, "https://example.com/p")
    pb = _page(db, b.id, "https://example.com/p")
    db.add(PageMetricDaily(site_id=a.id, page_id=pa, date=d, clicks=10, impressions=100, position=3.0))
    db.add(PageMetricDaily(site_id=b.id, page_id=pb, date=d, clicks=12, impressions=120, position=2.0))
    # a URL present only in property A -> should still appear
    pv = _page(db, a.id, "https://example.com/only-a")
    db.add(PageMetricDaily(site_id=a.id, page_id=pv, date=d, clicks=5, impressions=50, position=4.0))
    db.add(SiteTotalDaily(site_id=a.id, date=d, clicks=15, impressions=150, position=3.0))
    db.add(SiteTotalDaily(site_id=b.id, date=d, clicks=16, impressions=160, position=2.0))
    db.commit()
    dr = DateRange(d, d)

    # single property: unchanged behaviour
    assert T.site_totals(db, a.id, dr)["clicks"] == 15

    pages = {r["url"]: r for r in T.per_page_totals(db, [a.id, b.id], dr)}
    assert pages["https://example.com/p"]["clicks"] == 12        # max(10,12), not 22
    assert pages["https://example.com/p"]["impressions"] == 120  # max(100,120), not 220
    assert pages["https://example.com/only-a"]["clicks"] == 5

    st = T.site_totals(db, [a.id, b.id], dr)
    assert st["clicks"] == 16 and st["impressions"] == 160       # max per day, not 31/310
    daily = T.site_daily(db, [a.id, b.id], dr)
    assert len(daily) == 1 and daily[0]["clicks"] == 16


def test_dashboard_merge_toggle_renders(client, db):
    _site(db, "sc-domain:example.com")
    b = _site(db, "https://example.com/")
    r = client.get(f"/?site_id={b.id}&merge=1")
    assert r.status_code == 200
    assert "Объединено свойств одного домена" in r.text
