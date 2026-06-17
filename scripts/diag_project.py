"""Diagnose a project's clicks/impressions across periods (read-only).

Shows whether subset totals actually change with the date range, the real date
span of the project's page metrics, and how many Page rows the project's URLs
match — to tell a real bug from "no data that far back".

    python scripts/diag_project.py                 # list projects
    python scripts/diag_project.py --name бренды    # diagnose by (partial) name
    python scripts/diag_project.py --project-id 5
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402

from app.db.base import SessionLocal, init_db  # noqa: E402
from app.db.models import PageMetricDaily, Project, Site  # noqa: E402
from app.providers.base import DateRange  # noqa: E402
from app.services.loaders import project_page_ids  # noqa: E402
from app.services.totals import subset_totals  # noqa: E402
from app.utils import domain_of  # noqa: E402


def _same_domain_ids(db, site) -> list[int]:
    d = domain_of(site.property_uri)
    return [sid for sid, uri in db.execute(select(Site.id, Site.property_uri)).all()
            if d and domain_of(uri) == d] or [site.id]


def diag(db, project: Project) -> None:
    site = db.get(Site, project.site_id)
    print(f"\n=== Проект [{project.id}] «{project.name}» · сайт {site.property_uri} ===")
    print(f"URL в проекте: {len(project.urls)}")
    pids = project_page_ids(db, project.site_id, project)
    print(f"совпало Page-строк (этот сайт): {len(pids)}")
    pids_merge = project_page_ids(db, _same_domain_ids(db, site), project)
    print(f"совпало Page-строк (объединить домен): {len(pids_merge)}")

    span = db.execute(
        select(func.min(PageMetricDaily.date), func.max(PageMetricDaily.date), func.count())
        .where(PageMetricDaily.site_id == site.id)
    ).first()
    print(f"page_metric_daily сайта: даты {span[0]}…{span[1]}, строк {span[2]}")

    end = date.today() - timedelta(days=1)
    print("subset_totals по периодам (без объединения):")
    for label, days in (("7 дн", 7), ("30 дн", 30), ("90 дн", 90), ("365 дн", 365), ("730 дн", 730)):
        dr = DateRange(start=end - timedelta(days=days - 1), end=end)
        t = subset_totals(db, project, dr)
        print(f"   {label:>7} {dr.start}…{dr.end}:  клики={t['clicks']:>7}  показы={t['impressions']:>9}")
    print("   с объединением домена:")
    ids = _same_domain_ids(db, site)
    for label, days in (("30 дн", 30), ("365 дн", 365)):
        dr = DateRange(start=end - timedelta(days=days - 1), end=end)
        t = subset_totals(db, project, dr, site_ids=ids)
        print(f"   {label:>7} {dr.start}…{dr.end}:  клики={t['clicks']:>7}  показы={t['impressions']:>9}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-id", type=int)
    ap.add_argument("--name", help="часть названия проекта")
    a = ap.parse_args()
    init_db()
    db = SessionLocal()
    try:
        projects = db.execute(select(Project).order_by(Project.id)).scalars().all()
        if not a.project_id and not a.name:
            print("Проекты:")
            for p in projects:
                print(f"  [{p.id}] {p.name}  (URL: {len(p.urls)})")
            print("\nЗапусти с --name <часть> или --project-id N")
            return
        sel = [p for p in projects
               if (a.project_id and p.id == a.project_id)
               or (a.name and a.name.lower() in (p.name or "").lower())]
        if not sel:
            print("Проект не найден.")
            return
        for p in sel:
            diag(db, p)
    finally:
        db.close()


if __name__ == "__main__":
    main()
