"""Large projects: chunked IN keeps period filtering correct (SQLite var-limit safe).

Reproduces the bug where a project with many URLs (esp. with same-domain merge,
which multiplies page_ids) overflowed SQLite's bound-variable limit, so totals
stopped responding to the date range. The loaders now chunk the IN list.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.db.models import Page, PageMetricDaily, Project, ProjectUrl
from app.providers.base import DateRange
from app.services import loaders
from app.services.totals import subset_totals
from app.utils import normalize_url


def test_subset_totals_chunked_large_project(db, site):
    n = loaders._VAR_CHUNK + 200            # > one chunk → exercises chunk concatenation
    urls = [f"https://example.com/p{i}" for i in range(n)]
    p = Project(name="big", site_id=site.id)
    db.add(p)
    db.commit()
    db.execute(Page.__table__.insert(),
               [{"site_id": site.id, "url": u, "normalized_url": normalize_url(u)} for u in urls])
    db.execute(ProjectUrl.__table__.insert(),
               [{"project_id": p.id, "url": u, "normalized_url": normalize_url(u)} for u in urls])
    db.commit()
    pid = {nu: i for nu, i in db.execute(select(Page.normalized_url, Page.id)).all()}
    db.add_all([
        PageMetricDaily(site_id=site.id, page_id=pid[normalize_url(urls[0])],
                        date=date(2026, 6, 10), clicks=10, impressions=100, position=2.0),
        PageMetricDaily(site_id=site.id, page_id=pid[normalize_url(urls[1])],
                        date=date(2025, 1, 10), clicks=20, impressions=200, position=3.0),
    ])
    db.commit()

    def clicks(a, b):
        return subset_totals(db, p, DateRange(start=a, end=b))["clicks"]

    assert clicks(date(2026, 6, 1), date(2026, 6, 30)) == 10   # period respected, not frozen
    assert clicks(date(2025, 1, 1), date(2025, 1, 31)) == 20
    assert clicks(date(2024, 1, 1), date(2026, 6, 30)) == 30
    assert clicks(date(2023, 1, 1), date(2023, 12, 31)) == 0
