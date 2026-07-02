"""Merging same-domain properties (e.g. GSC https:// + sc-domain:) without
double-counting the overlap."""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import ensure_sources
from app.db.models import (
    DeviceMetricDaily,
    Page,
    PageMetricDaily,
    Project,
    ProjectUrl,
    Query,
    QueryMetricDaily,
    Site,
    SiteTotalDaily,
)
from app.providers import register_override
from app.providers.base import DateRange
from app.providers.mock import MockProvider
from app.services import totals as T
from app.services.antifraud import analyze
from app.services.ctr import ctr_for_project
from app.services.top_keyword import top_keywords_for_project
from app.utils import normalize_url, query_hash


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


def test_dashboard_picks_domain_and_merges(client, db):
    # two GSC properties of one domain -> a single bare-domain entry, auto-merged
    _site(db, "sc-domain:example.com")
    _site(db, "https://example.com/")
    # dashboard is progressive: data lives in the X-Partial fragment
    r = client.get("/?domain=example.com&engines=gsc", headers={"X-Partial": "1"})
    assert r.status_code == 200
    assert "example.com" in r.text          # bare domain in the picker (no protocol)
    assert "склеены" in r.text              # the auto-merge note


def test_bucket_series_week_month_and_last_snapshot():
    daily = [  # 2026-06-01 is a Monday; 06-01/06-02 same ISO week, 06-08 next
        {"date": "2026-06-01", "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 4.0},
        {"date": "2026-06-02", "clicks": 2, "impressions": 20, "ctr": 0.1, "position": 6.0},
        {"date": "2026-06-08", "clicks": 3, "impressions": 30, "ctr": 0.1, "position": 5.0},
    ]
    wk = T.bucket_series(daily, "week")
    assert [w["date"] for w in wk] == ["2026-06-01", "2026-06-08"]
    assert wk[0]["clicks"] == 3 and wk[0]["impressions"] == 30
    assert round(wk[0]["position"], 3) == round((4 * 10 + 6 * 20) / 30, 3)  # impression-weighted

    mo = T.bucket_series(daily, "month")
    assert len(mo) == 1 and mo[0]["date"] == "2026-06" and mo[0]["clicks"] == 6

    assert T.bucket_series(daily, "day") == daily  # unchanged

    snaps = [{"date": "2026-06-01", "count": 100}, {"date": "2026-06-02", "count": 130},
             {"date": "2026-06-08", "count": 90}]
    last = T.bucket_series(snaps, "week", agg="last")  # stock -> last of each bucket
    assert [s["count"] for s in last] == [130, 90]


def test_combine_across_engines_sums():
    # within an engine the loaders take max on overlap; ACROSS engines we sum,
    # with impression-weighted position.
    g = {"clicks": 100, "impressions": 1000, "ctr": 0.1, "position": 5.0}
    y = {"clicks": 30, "impressions": 300, "ctr": 0.1, "position": 9.0}
    c = T.combine_totals([g, y])
    assert c["clicks"] == 130 and c["impressions"] == 1300
    assert round(c["position"], 3) == round((5 * 1000 + 9 * 300) / 1300, 3)

    cd = T.combine_daily([
        [{"date": "2026-06-01", "clicks": 10, "impressions": 100, "ctr": 0.1, "position": 4.0}],
        [{"date": "2026-06-01", "clicks": 5, "impressions": 50, "ctr": 0.1, "position": 6.0}],
    ])
    assert len(cd) == 1 and cd[0]["clicks"] == 15 and cd[0]["impressions"] == 150

    cp = T.combine_pages([
        [{"url": "u", "clicks": 10, "impressions": 100, "ctr": 0.1, "position": 4.0}],
        [{"url": "u", "clicks": 5, "impressions": 50, "ctr": 0.1, "position": 6.0}],
    ])
    assert cp[0]["url"] == "u" and cp[0]["clicks"] == 15 and cp[0]["impressions"] == 150


def _query(db, site_id, text):
    q = Query(site_id=site_id, text=text, text_hash=query_hash(text))
    db.add(q)
    db.commit()
    return q.id


def test_project_services_merge(db):
    a, b = _site(db, "sc-domain:example.com"), _site(db, "https://example.com/")
    d, url = date(2026, 6, 1), "https://example.com/p"
    pa, pb = _page(db, a.id, url), _page(db, b.id, url)
    db.add_all([
        PageMetricDaily(site_id=a.id, page_id=pa, date=d, clicks=10, impressions=100, position=3.0),
        PageMetricDaily(site_id=b.id, page_id=pb, date=d, clicks=12, impressions=120, position=2.0),
    ])
    qa, qb = _query(db, a.id, "kw"), _query(db, b.id, "kw")
    db.add_all([
        QueryMetricDaily(site_id=a.id, page_id=pa, query_id=qa, date=d, clicks=10, impressions=100, position=3.0),
        QueryMetricDaily(site_id=b.id, page_id=pb, query_id=qb, date=d, clicks=12, impressions=120, position=2.0),
    ])
    proj = Project(name="P", site_id=a.id)
    db.add(proj)
    db.commit()
    db.add(ProjectUrl(project_id=proj.id, url=url, normalized_url=normalize_url(url)))
    db.commit()
    db.refresh(proj)
    dr, ids = DateRange(d, d), [a.id, b.id]

    ctr = ctr_for_project(db, proj, dr, site_ids=ids)
    assert ctr["totals"]["clicks"] == 12 and ctr["pages"][0]["clicks"] == 12  # max, not 22
    assert T.subset_totals(db, proj, dr, site_ids=ids)["clicks"] == 12
    tk = top_keywords_for_project(db, proj, dr, "clicks", site_ids=ids)
    assert tk[0]["top_query"] == "kw" and tk[0]["clicks"] == 12


def test_antifraud_merge_dedups_devices(db):
    a, b = _site(db, "sc-domain:example.com"), _site(db, "https://example.com/")
    d, url = date(2026, 6, 1), "https://example.com/p"
    pa, pb = _page(db, a.id, url), _page(db, b.id, url)
    db.add_all([
        DeviceMetricDaily(site_id=a.id, page_id=pa, date=d, device="desktop", clicks=5, impressions=100, position=3.0),
        DeviceMetricDaily(site_id=a.id, page_id=pa, date=d, device="mobile", clicks=4, impressions=90, position=3.0),
        DeviceMetricDaily(site_id=b.id, page_id=pb, date=d, device="desktop", clicks=6, impressions=120, position=2.0),
        DeviceMetricDaily(site_id=b.id, page_id=pb, date=d, device="mobile", clicks=5, impressions=110, position=2.0),
    ])
    db.commit()
    summ = analyze(db, [a.id, b.id], DateRange(d, d), ratio_threshold=10.0, min_impressions=50)["summary"]
    # max per (url, device, day): desktop 120 + mobile 110 = 230, not 100+120+90+110
    assert summ["raw_impressions"] == 230


def test_project_page_combines_same_domain(client, db):
    a = _site(db, "sc-domain:example.com")
    b = _site(db, "https://example.com/")
    d, url = date(2026, 6, 1), "https://example.com/p"
    pa, pb = _page(db, a.id, url), _page(db, b.id, url)
    db.add_all([
        PageMetricDaily(site_id=a.id, page_id=pa, date=d, clicks=10, impressions=100, position=2.0),
        PageMetricDaily(site_id=b.id, page_id=pb, date=d, clicks=12, impressions=120, position=2.0),
    ])
    proj = Project(name="P", site_id=a.id)
    db.add(proj)
    db.commit()
    db.add(ProjectUrl(project_id=proj.id, url=url, normalized_url=normalize_url(url)))
    db.commit()
    r = client.get(f"/projects/{proj.id}?start={d}&end={d}")
    assert r.status_code == 200
    assert "Поисковая система" in r.text          # engine selector replaces the merge checkbox
    # same-domain GSC twins are de-duped within the engine: max(10,12)=12, not 22
    assert "<h3>12</h3>" in r.text


def test_project_page_combines_engines(client, db):
    g = _site(db, "https://example.com/")                       # GSC property
    yw = ensure_sources(db)["yandex_webmaster"]
    y = Site(source_id=yw.id, property_uri="https://example.com/", display_name="y")
    db.add(y)
    db.commit()
    d, url = date(2026, 6, 1), "https://example.com/p"
    pg, py = _page(db, g.id, url), _page(db, y.id, url)
    db.add_all([
        PageMetricDaily(site_id=g.id, page_id=pg, date=d, clicks=10, impressions=100, position=2.0),
        PageMetricDaily(site_id=y.id, page_id=py, date=d, clicks=7, impressions=70, position=3.0),
    ])
    proj = Project(name="P", site_id=g.id)
    db.add(proj)
    db.commit()
    db.add(ProjectUrl(project_id=proj.id, url=url, normalized_url=normalize_url(url)))
    db.commit()
    base = f"/projects/{proj.id}?start={d}&end={d}"
    assert "<h3>17</h3>" in client.get(base).text                       # both engines summed (10+7)
    assert "<h3>10</h3>" in client.get(base + "&engines=gsc").text       # only Google
    assert "<h3>7</h3>" in client.get(base + "&engines=yandex_webmaster").text  # only Yandex
    page = client.get(base).text
    assert "Google" in page and "Яндекс" in page                        # both selectable
