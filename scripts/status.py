"""One-shot status: what data is in the DB and what was collected.

    python scripts/status.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402

from app.db.base import SessionLocal, init_db  # noqa: E402
from app.db.models import (  # noqa: E402
    CollectionRun,
    DeviceMetricDaily,
    IndexedUrlSnapshot,
    PageMetricDaily,
    QueryMetricDaily,
    Site,
    SiteTotalDaily,
    Source,
)


def _agg(db, model, date_col):
    lo, hi, c, sites = db.execute(
        select(func.min(date_col), func.max(date_col), func.count(), func.count(func.distinct(model.site_id)))
    ).one()
    return lo, hi, c or 0, sites or 0


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        print("=== Источники / сайты ===")
        for src in db.execute(select(Source).order_by(Source.id)).scalars():
            total = db.execute(select(func.count()).select_from(Site).where(Site.source_id == src.id)).scalar_one()
            en = db.execute(
                select(func.count()).select_from(Site).where(Site.source_id == src.id, Site.enabled.is_(True))
            ).scalar_one()
            print(f"  {src.code:<18} сайтов: {total:<4} включено: {en}")

        print("\n=== Данные (период · строк · сайтов) ===")
        tables = [
            ("Итоги по сайту (totals)", SiteTotalDaily, SiteTotalDaily.date),
            ("По страницам (page)", PageMetricDaily, PageMetricDaily.date),
            ("По запросам (query)", QueryMetricDaily, QueryMetricDaily.date),
            ("По устройствам (device)", DeviceMetricDaily, DeviceMetricDaily.date),
            ("Страницы в индексе (snapshots)", IndexedUrlSnapshot, IndexedUrlSnapshot.captured_on),
        ]
        for label, model, col in tables:
            lo, hi, c, sites = _agg(db, model, col)
            rng = f"{lo}..{hi}" if c else "—"
            print(f"  {label:<34} {rng:<26} {c:>9} строк · {sites} сайтов")

        print("\n=== Запуски сбора ===")
        for st in ("ok", "error", "pending"):
            n = db.execute(select(func.count()).select_from(CollectionRun).where(CollectionRun.status == st)).scalar_one()
            print(f"  {st:<10} {n}")
        print("  последние 6:")
        runs = db.execute(select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(6)).scalars().all()
        for r in runs:
            err = f" — {r.error_text[:50]}" if r.error_text else ""
            print(f"    site {r.site_id:<3} {r.job_type:<9} {str(r.target_date):<12} {r.status:<7} {r.rows_written:>7} rows{err}")

        print("\n=== Снимки индекса ===")
        sites_n, dates_n, lo, hi, total = db.execute(
            select(
                func.count(func.distinct(IndexedUrlSnapshot.site_id)),
                func.count(func.distinct(IndexedUrlSnapshot.captured_on)),
                func.min(IndexedUrlSnapshot.captured_on),
                func.max(IndexedUrlSnapshot.captured_on),
                func.count(),
            )
        ).one()
        if total:
            print(f"  сайтов: {sites_n} · дат(снимков): {dates_n} ({lo}..{hi}) · всего URL: {total}")
        else:
            print("  снимков пока нет")
    finally:
        db.close()


if __name__ == "__main__":
    main()
