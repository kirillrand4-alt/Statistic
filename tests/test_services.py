"""Unit tests for ingestion idempotency and the analytical services."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select

from app.db.models import PageMetricDaily
from app.providers.base import DateRange
from app.scheduler.jobs import collect_site
from app.services import growth
from app.services.ctr import ctr_for_project
from app.services.top_keyword import top_keywords_for_project


def test_connect_gsc_service_account_autopull(db):
    import json

    from sqlalchemy import func, select

    from app.db.models import PageMetricDaily, QueryMetricDaily, Site
    from app.services.connect import connect_gsc_service_account

    key = json.dumps(
        {
            "type": "service_account",
            "client_email": "svc@proj.iam.gserviceaccount.com",
            "private_key": "-----BEGIN PRIVATE KEY-----\nAA\n-----END PRIVATE KEY-----\n",
        }
    )
    result = connect_gsc_service_account(db, key, backfill_days=20, background=False)

    assert result["site_ids"], "a site should be auto-registered from list_sites()"
    assert db.execute(select(func.count()).select_from(Site)).scalar_one() >= 1
    # data was pulled automatically (no project needed)
    assert db.execute(select(func.count()).select_from(PageMetricDaily)).scalar_one() > 0
    assert db.execute(select(func.count()).select_from(QueryMetricDaily)).scalar_one() > 0


def test_connect_gsc_oauth_autopull(db):
    from sqlalchemy import func, select

    from app.db.models import PageMetricDaily, Site
    from app.services.connect import connect_gsc_oauth

    result = connect_gsc_oauth(
        db, "client-id", "client-secret", "refresh-token", backfill_days=20, background=False
    )
    assert result["site_ids"]
    assert db.execute(select(func.count()).select_from(Site)).scalar_one() >= 1
    assert db.execute(select(func.count()).select_from(PageMetricDaily)).scalar_one() > 0


def test_yandex_query_analytics_parsing():
    from datetime import date as _date

    from app.providers.base import DateRange
    from app.providers.yandex_webmaster import YandexWebmasterProvider

    class _Site:
        property_uri = "https://prokompressor.ru/"
        external_host_id = "https:prokompressor.ru:443"

    p = YandexWebmasterProvider()
    p._uid = 1
    dr = DateRange(start=_date(2026, 5, 25), end=_date(2026, 5, 25))

    p._post = lambda path, body: {
        "count": 1,
        "text_indicator_to_statistics": [
            {
                "text_indicator": {"type": "QUERY", "value": "compressor buy"},
                "popular_complementary_indicator": {"type": "URL", "value": "/catalog/"},
                "statistics": [
                    {"date": "2026-05-25", "field": "IMPRESSIONS", "value": 100.0},
                    {"date": "2026-05-25", "field": "CLICKS", "value": 10.0},
                    {"date": "2026-05-25", "field": "POSITION", "value": 3.5},
                ],
            }
        ],
    }
    rows = list(p.fetch_all_query_metrics(_Site(), dr))
    assert len(rows) == 1
    assert rows[0].query == "compressor buy"
    assert rows[0].url == "https://prokompressor.ru/catalog/"
    assert rows[0].clicks == 10 and rows[0].impressions == 100
    assert abs(rows[0].position - 3.5) < 1e-9

    p._post = lambda path, body: {
        "text_indicator_to_statistics": [
            {
                "text_indicator": {"type": "URL", "value": "/catalog/"},
                "statistics": [
                    {"date": "2026-05-25", "field": "IMPRESSIONS", "value": 200.0},
                    {"date": "2026-05-25", "field": "CLICKS", "value": 20.0},
                ],
            }
        ],
    }
    prows = list(p.fetch_page_metrics(_Site(), dr))
    assert len(prows) == 1
    assert prows[0].url == "https://prokompressor.ru/catalog/"
    assert prows[0].clicks == 20 and prows[0].impressions == 200


def test_connect_yandex_autopull(db):
    from sqlalchemy import func, select

    from app.db.models import Site, SiteTotalDaily, Source
    from app.providers import register_override
    from app.providers.mock import MockProvider
    from app.services.connect import connect_yandex

    register_override("yandex_webmaster", MockProvider())
    result = connect_yandex(db, "y0_testtoken", backfill_days=20, background=False)

    assert result["site_ids"]
    ywm = db.execute(select(Source).where(Source.code == "yandex_webmaster")).scalar_one()
    n_sites = db.execute(
        select(func.count()).select_from(Site).where(Site.source_id == ywm.id)
    ).scalar_one()
    assert n_sites >= 1
    assert db.execute(select(func.count()).select_from(SiteTotalDaily)).scalar_one() > 0


def test_connect_rejects_incomplete(db):
    import pytest

    from app.services.connect import connect_gsc_oauth, connect_gsc_service_account

    with pytest.raises(Exception):
        connect_gsc_service_account(db, "{ not json", background=False)
    with pytest.raises(Exception):
        connect_gsc_oauth(db, "", "", "", background=False)


def test_credentials_roundtrip(db):
    from app.credentials import get_cred, set_cred

    set_cred("k", "secret-value")
    assert get_cred("k") == "secret-value"
    assert get_cred("missing", "def") == "def"


def test_domain_of():
    from app.utils import domain_of

    assert domain_of("sc-domain:example.com") == "example.com"
    assert domain_of("https://www.example.com/") == "example.com"
    assert domain_of("https://example.com:443/") == "example.com"


def test_multi_compare_project(db, site, project):
    from datetime import date, timedelta

    from app.bootstrap import ensure_sources
    from app.db.models import Page, PageMetricDaily, Site
    from app.providers.base import DateRange
    from app.services.multi_compare import compare_project

    ywm = ensure_sources(db)["yandex_webmaster"]
    ysite = Site(source_id=ywm.id, property_uri="https://example.com/", display_name="ya")
    db.add(ysite)
    db.commit()

    pu = project.urls[0]
    today = date.today()
    d_a, d_b = today - timedelta(days=2), today - timedelta(days=9)
    # gsc: 100 -> 50 clicks (рост в A); yandex: 30 -> 60 (падение в A)
    for s_, clk_a, clk_b in ((site, 100, 50), (ysite, 30, 60)):
        pg = Page(site_id=s_.id, url=pu.url, normalized_url=pu.normalized_url)
        db.add(pg)
        db.commit()
        db.add_all([
            PageMetricDaily(site_id=s_.id, page_id=pg.id, date=d_a, clicks=clk_a, impressions=1000, position=5.0),
            PageMetricDaily(site_id=s_.id, page_id=pg.id, date=d_b, clicks=clk_b, impressions=900, position=6.0),
        ])
        db.commit()

    period_a = DateRange(start=today - timedelta(days=5), end=today - timedelta(days=1))
    period_b = DateRange(start=today - timedelta(days=12), end=today - timedelta(days=6))
    res = compare_project(db, project, "clicks", period_a, period_b)

    assert set(res["engines"]) == {"gsc", "yandex_webmaster"}
    row = next(r for r in res["rows"] if r["url"] == pu.url)
    g, y = row["engines"]["gsc"], row["engines"]["yandex_webmaster"]
    assert (g["a"], g["b"], g["delta"], g["improved"]) == (100, 50, 50, True)
    assert (y["a"], y["b"], y["delta"], y["improved"]) == (30, 60, -30, False)
    assert res["engines"]["gsc"]["a"] == 100 and res["engines"]["yandex_webmaster"]["b"] == 60
    # URLs without data still present, zero-filled
    assert len(res["rows"]) == len(project.urls)


def test_multi_compare_exclude_bots(db, site, project):
    from datetime import date, timedelta

    from app.db.models import DeviceMetricDaily, Page
    from app.providers.base import DateRange
    from app.services.multi_compare import compare_project

    pu = project.urls[0]
    today = date.today()
    d_a = today - timedelta(days=2)
    pg = Page(site_id=site.id, url=pu.url, normalized_url=pu.normalized_url)
    db.add(pg)
    db.commit()
    # desktop 1000 (bots) vs mobile 50 in period A -> clean clicks = mobile only
    db.add_all([
        DeviceMetricDaily(site_id=site.id, page_id=pg.id, date=d_a, device="desktop", clicks=200, impressions=1000),
        DeviceMetricDaily(site_id=site.id, page_id=pg.id, date=d_a, device="mobile", clicks=5, impressions=50),
    ])
    db.commit()

    period_a = DateRange(start=today - timedelta(days=5), end=today - timedelta(days=1))
    period_b = DateRange(start=today - timedelta(days=12), end=today - timedelta(days=6))
    res = compare_project(db, project, "clicks", period_a, period_b,
                          exclude_bots=True, ratio=10.0, min_impr=100)
    row = next(r for r in res["rows"] if r["url"] == pu.url)
    assert row["engines"]["gsc"]["a"] == 5  # desktop (bot) removed, only mobile clicks remain
    assert res["exclude_bots"] is True


def test_per_page_with_devices(db, site):
    from datetime import date, timedelta

    from app.db.models import DeviceMetricDaily, Page, PageMetricDaily
    from app.providers.base import DateRange
    from app.services import totals as totals_svc

    d = date.today() - timedelta(days=1)
    pg = Page(site_id=site.id, url="https://x/p", normalized_url="https://x/p")
    db.add(pg)
    db.commit()
    db.add(PageMetricDaily(site_id=site.id, page_id=pg.id, date=d, clicks=30, impressions=300, position=4.0))
    db.add_all([
        DeviceMetricDaily(site_id=site.id, page_id=pg.id, date=d, device="desktop", clicks=20, impressions=200),
        DeviceMetricDaily(site_id=site.id, page_id=pg.id, date=d, device="mobile", clicks=10, impressions=100),
    ])
    db.commit()

    rows = totals_svc.per_page_with_devices(db, site.id, DateRange(start=d, end=d))
    r = next(x for x in rows if x["url"] == "https://x/p")
    assert r["desktop_impr"] == 200 and r["mobile_impr"] == 100
    assert abs(r["desktop_ctr"] - 0.1) < 1e-9 and abs(r["mobile_ctr"] - 0.1) < 1e-9


def test_antifraud_analyze(db, site):
    from datetime import date, timedelta

    from app.db.models import DeviceMetricDaily, Page
    from app.providers.base import DateRange
    from app.services.antifraud import analyze

    d = date.today() - timedelta(days=1)
    flagged = Page(site_id=site.id, url="https://x/bots", normalized_url="https://x/bots")
    noise = Page(site_id=site.id, url="https://x/quiet", normalized_url="https://x/quiet")
    db.add_all([flagged, noise])
    db.commit()
    db.add_all([
        # desktop 1000 vs mobile 50 -> ratio 20, total 1050 > 100 -> flagged, bot=desktop
        DeviceMetricDaily(site_id=site.id, page_id=flagged.id, date=d, device="desktop", clicks=10, impressions=1000),
        DeviceMetricDaily(site_id=site.id, page_id=flagged.id, date=d, device="mobile", clicks=5, impressions=50),
        # total 72 <= 100 -> noise, not flagged even though ratio is huge
        DeviceMetricDaily(site_id=site.id, page_id=noise.id, date=d, device="desktop", clicks=1, impressions=70),
        DeviceMetricDaily(site_id=site.id, page_id=noise.id, date=d, device="mobile", clicks=0, impressions=2),
    ])
    db.commit()

    res = analyze(db, site.id, DateRange(start=d, end=d), ratio_threshold=10.0, min_impressions=100)
    assert res["summary"]["flagged"] == 1
    row = res["rows"][0]
    assert row["url"] == "https://x/bots"
    assert row["bot_device"] == "desktop"
    assert row["clean_impr"] == 50
    assert res["summary"]["removed_impressions"] == 1000


def test_indexing_capture_and_compare(db, site):
    from datetime import date, timedelta

    from app.db.models import IndexedUrlSnapshot
    from app.services import indexing

    n = indexing.capture_indexed_urls(db, site)  # mock -> DEFAULT_PAGES
    assert n >= 1
    snaps = indexing.list_snapshots(db, site.id)
    assert snaps and snaps[0]["count"] == n

    # fabricate an older snapshot missing one URL and with an extra dropped one
    older = date.today() - timedelta(days=7)
    today_urls = {s.normalized_url for s in db.execute(
        select(IndexedUrlSnapshot).where(IndexedUrlSnapshot.captured_on == date.today())
    ).scalars()}
    keep = list(today_urls)[:-1]  # drop one -> it "entered" today
    db.add_all([IndexedUrlSnapshot(site_id=site.id, captured_on=older, url=u, normalized_url=u) for u in keep])
    db.add(IndexedUrlSnapshot(site_id=site.id, captured_on=older, url="https://gone", normalized_url="https://gone"))
    db.commit()

    cmp = indexing.compare_snapshots(db, site.id, date.today(), older)
    assert len(cmp["added"]) == 1     # the URL not in the older snapshot
    assert any(r["url"] == "https://gone" for r in cmp["removed"])


def test_base_path_normalization():
    from app.config import Settings

    assert Settings(root_path="").base_path == ""
    assert Settings(root_path="/stat").base_path == "/stat"
    assert Settings(root_path="stat/").base_path == "/stat"
    assert Settings(root_path="/stat/").base_path == "/stat"


def _range(days_back_start, days_back_end):
    end = date.today() - timedelta(days=days_back_end)
    start = date.today() - timedelta(days=days_back_start)
    return DateRange(start=start, end=end)


def test_ingest_is_idempotent(db, site, project):
    dr = _range(20, 1)
    collect_site(db, site, dr)
    count1 = db.execute(select(func.count()).select_from(PageMetricDaily)).scalar_one()
    # Re-running the same window must overwrite, not duplicate.
    collect_site(db, site, dr)
    count2 = db.execute(select(func.count()).select_from(PageMetricDaily)).scalar_one()
    assert count1 > 0
    assert count1 == count2


def test_top_keyword_picks_a_query(db, site, project):
    dr = _range(20, 1)
    collect_site(db, site, dr)
    items = top_keywords_for_project(db, project, dr, order_by="clicks")
    assert len(items) == len(project.urls)
    with_data = [i for i in items if i["top_query"]]
    assert with_data, "expected at least one URL with a TOP keyword"
    # clicks-ordered: the top row's clicks must be >= 0 and ctr consistent
    row = with_data[0]
    assert row["impressions"] >= row["clicks"] >= 0


def test_ctr_recomputed_from_sums(db, site, project):
    dr = _range(20, 1)
    collect_site(db, site, dr)
    res = ctr_for_project(db, project, dr)
    t = res["totals"]
    if t["impressions"]:
        assert abs(t["ctr"] - t["clicks"] / t["impressions"]) < 1e-9


def test_growth_compare_formula(db, site, project):
    a = _range(10, 1)   # recent 10 days
    b = _range(20, 11)  # prior 10 days
    collect_site(db, site, DateRange(start=b.start, end=a.end))
    res = growth.compare(db, site.id, "clicks", period_a=a, period_b=b, grouping="site")
    s = res["summary"]
    assert s["delta"] == s["value_a"] - s["value_b"]
    if s["value_b"]:
        assert abs(s["pct_change"] - (s["delta"] / s["value_b"] * 100)) < 1e-6


def test_growth_position_direction(db, site, project):
    a = _range(10, 1)
    b = _range(20, 11)
    collect_site(db, site, DateRange(start=b.start, end=a.end))
    res = growth.compare(db, site.id, "position", period_a=a, period_b=b, grouping="page")
    for row in res["rows"]:
        if row["value_a"] > 0 and row["value_b"] > 0:
            # improved == moved to a lower (better) position
            assert row["improved"] == (row["value_a"] < row["value_b"])
