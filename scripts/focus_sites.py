"""Keep only the sites you care about enabled, disable the rest, and backfill them.

    python scripts/focus_sites.py prokompressor.ru enger-air.ru [days]

Matches sites whose property URL contains any given substring (across GSC and
Yandex), enables only those, disables everything else, then runs a newest-first
backfill for the kept sites. An optional trailing integer sets the history
window in days (default 120).
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
    args = sys.argv[1:]
    days = 120
    if args and args[-1].isdigit():
        days = int(args[-1])
        args = args[:-1]
    needles = [a.lower() for a in args]
    if not needles:
        print("Usage: python scripts/focus_sites.py <substr> [<substr> ...] [days]")
        return

    init_db()
    db = SessionLocal()
    try:
        sites = db.execute(select(Site)).scalars().all()
        kept = []
        for s in sites:
            match = any(n in (s.property_uri or "").lower() for n in needles)
            s.enabled = match
            if match:
                kept.append(s)
        db.commit()

        print(f"Enabled {len(kept)} site(s), disabled {len(sites) - len(kept)}:")
        for s in kept:
            print(f"  + {s.source.code}: {s.property_uri}")
        if not kept:
            print("Nothing matched — check the substrings against /api/sites.")
            return

        for s in kept:
            print(f"Backfilling {days}d (newest first): {s.source.code} {s.property_uri} ...")
            try:
                rows = run_backfill(db, s.id, days=days)
                print(f"   ok, {rows} rows")
            except Exception as exc:  # noqa: BLE001
                print(f"   ERROR: {exc}")
        print("Done. Open the dashboard and pick one of these sites.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
