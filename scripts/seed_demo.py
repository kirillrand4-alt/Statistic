"""Seed the DB with demo data via the MockProvider — no API keys needed.

    python scripts/seed_demo.py

Registers the MockProvider under the "gsc" source code, creates a demo site +
project, and ingests ~60 days of synthetic metrics so the whole UI works.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.bootstrap import ensure_sources  # noqa: E402
from app.db.base import SessionLocal, init_db  # noqa: E402
from app.db.models import Project, ProjectUrl, Site  # noqa: E402
from app.providers import register_override  # noqa: E402
from app.providers.base import DateRange  # noqa: E402
from app.providers.mock import DEFAULT_PAGES, MockProvider  # noqa: E402
from app.scheduler.jobs import collect_site  # noqa: E402
from app.utils import normalize_url  # noqa: E402


def main() -> None:
    init_db()
    register_override("gsc", MockProvider())
    db = SessionLocal()
    try:
        gsc = ensure_sources(db)["gsc"]
        site = db.execute(
            select(Site).where(
                Site.source_id == gsc.id, Site.property_uri == "sc-domain:example.com"
            )
        ).scalar_one_or_none()
        if site is None:
            site = Site(
                source_id=gsc.id,
                property_uri="sc-domain:example.com",
                display_name="example.com (демо)",
            )
            db.add(site)
            db.commit()

        proj = db.execute(
            select(Project).where(Project.name == "Демо-проект")
        ).scalar_one_or_none()
        if proj is None:
            proj = Project(name="Демо-проект", site_id=site.id)
            db.add(proj)
            db.commit()
            for u in DEFAULT_PAGES:
                db.add(ProjectUrl(project_id=proj.id, url=u, normalized_url=normalize_url(u)))
            db.commit()

        end = date.today() - timedelta(days=1)
        dr = DateRange(start=end - timedelta(days=60), end=end)
        rows = collect_site(db, site, dr, job_type="backfill")
        print(f"Seeded {rows} rows for site {site.id} ({dr.start}..{dr.end}).")
        print("Run: uvicorn app.main:app --reload  then open http://localhost:8000")
    finally:
        db.close()


if __name__ == "__main__":
    main()
