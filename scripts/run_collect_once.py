"""Run a one-off collection for a site using the REAL providers.

    python scripts/run_collect_once.py --site-id 1 --days 7

Requires valid credentials in .env (e.g. GSC service account). Useful to verify
the integration end-to-end and to inspect rows landing in the DB.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.bootstrap import bootstrap  # noqa: E402
from app.db.base import SessionLocal, init_db  # noqa: E402
from app.db.models import Site  # noqa: E402
from app.providers.base import DateRange  # noqa: E402
from app.scheduler.jobs import collect_site  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site-id", type=int, default=None)
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        bootstrap(db)
        site = (
            db.get(Site, args.site_id)
            if args.site_id
            else db.execute(select(Site).order_by(Site.id).limit(1)).scalar_one_or_none()
        )
        if site is None:
            print("No site found. Set GSC_SITE_URL in .env or create a site first.")
            return
        end = date.today() - timedelta(days=1)
        dr = DateRange(start=end - timedelta(days=args.days), end=end)
        rows = collect_site(db, site, dr)
        print(f"Collected {rows} rows for site {site.id} ({dr.start}..{dr.end}).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
