"""Per-brand favourite-goal completions on the project page."""
from __future__ import annotations

from datetime import date

from app.api.routes_pages import _brand_goals
from app.db.models import Project, ProjectUrl, UrlBrand, Visit
from app.providers.base import DateRange
from app.services.goals import page_key

DR = DateRange(start=date(2026, 6, 1), end=date(2026, 6, 30))


def test_brand_goals_groups_by_landing_brand(db, site):
    u1, u2 = "https://example.com/a", "https://example.com/b"
    p = Project(name="P", site_id=site.id, favorite_goals="123")
    db.add(p)
    db.commit()
    db.add_all([
        ProjectUrl(project_id=p.id, url=u1, normalized_url=u1),
        ProjectUrl(project_id=p.id, url=u2, normalized_url=u2),
        UrlBrand(domain="example.com", url_key=page_key(u1), brand="Aso", url=u1),
        UrlBrand(domain="example.com", url_key=page_key(u2), brand="Berg", url=u2),
        # two visits landing on Aso's page complete favourite goal 123; one is goal 999 (ignored)
        Visit(site_id=site.id, visit_id="v1", date=date(2026, 6, 10), counter_id=99,
              start_url=u1, extra='{"goalsID":"123"}'),
        Visit(site_id=site.id, visit_id="v2", date=date(2026, 6, 11), counter_id=99,
              start_url=u1, extra='{"goalsID":"[123,5]"}'),
        Visit(site_id=site.id, visit_id="v3", date=date(2026, 6, 12), counter_id=99,
              start_url=u2, extra='{"goalsID":"999"}'),
    ])
    db.commit()

    res = _brand_goals(db, p, site, DR, "example.com")
    assert res is not None
    counts = {r["brand"]: r["goals"] for r in res["rows"]}
    assert counts["Aso"] == 2          # two visits hit favourite goal 123 on Aso's page
    assert counts["Berg"] == 0         # goal 999 isn't a favourite
    assert res["total"] == 2
    # all brands present, sorted with the top brand first
    assert res["rows"][0]["brand"] == "Aso"


def test_brand_goals_none_without_brand_map(db, site):
    p = Project(name="P2", site_id=site.id)
    db.add(p)
    db.commit()
    assert _brand_goals(db, p, site, DR, "example.com") is None
