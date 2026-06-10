"""Show data coverage per enabled site: page metrics vs device (mobile/desktop).

    python scripts/check_coverage.py

For each site prints the date range and row count of page metrics and of
device-split metrics, so you can see where mobile/desktop data is missing.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402

from app.db.base import SessionLocal, init_db  # noqa: E402
from app.db.models import DeviceMetricDaily, PageMetricDaily, Site  # noqa: E402


def _range(db, model, site_id):
    lo, hi, c = db.execute(
        select(func.min(model.date), func.max(model.date), func.count()).where(model.site_id == site_id)
    ).one()
    return lo, hi, c or 0


def _fmt(lo, hi, c):
    if not c:
        return "—"
    return f"{lo}..{hi} ({c} rows)"


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        sites = db.execute(select(Site).where(Site.enabled.is_(True)).order_by(Site.id)).scalars().all()
        print(f"{'source':<18}{'site':<42}{'page metrics':<30}device metrics")
        print("-" * 120)
        for s in sites:
            p = _range(db, PageMetricDaily, s.id)
            d = _range(db, DeviceMetricDaily, s.id)
            print(f"{s.source.code:<18}{s.property_uri[:40]:<42}{_fmt(*p):<30}{_fmt(*d)}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
