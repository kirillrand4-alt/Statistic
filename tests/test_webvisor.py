"""Webvisor session selection (sizing) from synced visits."""
from __future__ import annotations

from datetime import date

from app.db.models import Visit
from app.providers.base import DateRange
from app.services import webvisor as W
from app.utils import domain_of

DR = DateRange(start=date(2026, 6, 1), end=date(2026, 6, 30))


def _seed(db, site):
    db.add_all([
        Visit(site_id=site.id, visit_id="v1", date=date(2026, 6, 10), traffic_source="organic",
              page_views=5, duration=120, start_url="https://example.com/a"),
        Visit(site_id=site.id, visit_id="v2", date=date(2026, 6, 11), traffic_source="ad",
              page_views=1, duration=5, start_url="https://example.com/b"),
        Visit(site_id=site.id, visit_id="v4", date=date(2026, 6, 12), traffic_source="organic",
              page_views=2, duration=90),
        Visit(site_id=site.id, visit_id="v3", date=date(2026, 5, 1), traffic_source="organic",
              page_views=3, duration=60),                                   # out of range
    ])
    db.commit()


def test_count_list_and_filters(db, site):
    _seed(db, site)
    assert W.count_sessions(db, site.id, DR) == 3                           # v1,v2,v4
    assert [r["visit_id"] for r in W.sessions_for_period(db, site.id, DR)] == ["v4", "v2", "v1"]
    assert W.count_sessions(db, site.id, DR, source="organic") == 2         # v1,v4
    assert W.count_sessions(db, site.id, DR, min_page_views=3) == 1         # v1
    assert W.sessions_for_period(db, site.id, DR, limit=1)[0]["visit_id"] == "v4"


def test_duration_filter(db, site):
    _seed(db, site)
    # «дольше 10 секунд»: v1(120), v4(90); v2(5) отсекается
    assert sorted(r["visit_id"] for r in
                  W.sessions_for_period(db, site.id, DR, min_duration=10)) == ["v1", "v4"]
    assert W.count_sessions(db, site.id, DR, min_duration=10) == 2


def test_resolve_visit_site(db, site):
    _seed(db, site)
    assert W.resolve_visit_site(db, site_id=site.id) == site.id
    assert W.resolve_visit_site(db, domain=domain_of(site.property_uri)) == site.id
