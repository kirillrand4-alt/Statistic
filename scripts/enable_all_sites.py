"""Enable ALL sites again and backfill them.

    python scripts/enable_all_sites.py [days]

Sets every site to enabled (undo of focus_sites.py) and runs a newest-first
backfill for each. Pass 0 to only enable without backfilling (the daily
scheduler will then pick them up). Default window: 30 days.

Heavy with many sites — run detached:
    nohup .venv/bin/python scripts/enable_all_sites.py 30 > /tmp/collect_all.log 2>&1 &
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.db.base import SessionLocal, init_db  # noqa: E402
from app.db.models import Site  # noqa: E402
from app.scheduler.jobs import run_backfill  # noqa: E402


def main() -> None:
    days = 30
    if len(sys.argv) > 1 and sys.argv[1].lstrip("-").isdigit():
        days = int(sys.argv[1])

    init_db()
    db = SessionLocal()
    try:
        sites = db.execute(select(Site).order_by(Site.id)).scalars().all()
        for s in sites:
            s.enabled = True
        db.commit()
        print(f"Enabled {len(sites)} site(s).")

        if days <= 0:
            print("Backfill skipped (days<=0). Daily scheduler will collect them.")
            return

        for i, s in enumerate(sites, 1):
            print(f"[{i}/{len(sites)}] {days}d {s.source.code}: {s.property_uri} ...")
            try:
                rows = run_backfill(db, s.id, days=days)
                print(f"    ok, {rows} rows")
            except Exception as exc:  # noqa: BLE001
                print(f"    ERROR: {exc}")
        print("Done.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
