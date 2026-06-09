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
