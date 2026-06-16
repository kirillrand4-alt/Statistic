"""Per-brand search clicks + favourite-goal completions on the project page."""
from __future__ import annotations

from datetime import date

from app.api.routes_pages import _brand_goals
from app.db.models import Page, PageMetricDaily, Project, ProjectUrl, UrlBrand, Visit
from app.providers.base import DateRange
from app.services.goals import page_key
from app.utils import normalize_url

DR = DateRange(start=date(2026, 6, 1), end=date(2026, 6, 30))


def test_brand_goals_and_clicks_by_brand(db, site):
    u1, u2 = "https://example.com/a", "https://example.com/b"
    n1, n2 = normalize_url(u1), normalize_url(u2)
    p = Project(name="P", site_id=site.id, favorite_goals="123")
    db.add(p)
    db.commit()
    pg1 = Page(site_id=site.id, url=u1, normalized_url=n1)
    db.add(pg1)
    db.commit()
    db.add_all([
        ProjectUrl(project_id=p.id, url=u1, normalized_url=n1),
        ProjectUrl(project_id=p.id, url=u2, normalized_url=n2),
        UrlBrand(domain="example.com", url_key=page_key(u1), brand="Aso", url=u1),
        UrlBrand(domain="example.com", url_key=page_key(u2), brand="Berg", url=u2),
        PageMetricDaily(site_id=site.id, page_id=pg1.id, date=date(2026, 6, 10),
                        clicks=5, impressions=100, position=3.0),
        # two visits land on Aso's page and reach favourite goal 123; goal 999 ignored
        Visit(site_id=site.id, visit_id="v1", date=date(2026, 6, 10), counter_id=99,
              start_url=u1, extra='{"goalsID":"123"}'),
        Visit(site_id=site.id, visit_id="v2", date=date(2026, 6, 11), counter_id=99,
              start_url=u1, extra='{"goalsID":"[123,5]"}'),
        Visit(site_id=site.id, visit_id="v3", date=date(2026, 6, 12), counter_id=99,
              start_url=u2, extra='{"goalsID":"999"}'),
    ])
    db.commit()

    res = _brand_goals(db, p, site, DR, "example.com")
    by = {r["brand"]: r for r in res["rows"]}
    assert by["Aso"]["goals"] == 2 and by["Aso"]["clicks"] == 5
    assert by["Aso"]["conv"] == 40.0                 # 2 goals / 5 clicks * 100
    assert by["Berg"]["goals"] == 0 and by["Berg"]["clicks"] == 0
    assert by["Berg"]["conv"] is None                # no clicks → no conversion
    assert res["total"] == 2 and res["total_clicks"] == 5
    assert res["rows"][0]["brand"] == "Aso"          # sorted: top goals first


def test_brand_goals_none_without_brand_map(db, site):
    p = Project(name="P2", site_id=site.id)
    db.add(p)
    db.commit()
    assert _brand_goals(db, p, site, DR, "example.com") is None
