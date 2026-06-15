"""Report keyword cannibalization (pages of one site competing for a query).

Uses data already collected: GSC ``page × query × day`` (query_metric_daily) and,
for confirmation, the stored SERP (serp_result, arsenkin). Read-only.

Examples:
    # preview to console
    python scripts/cannibalization.py --domain prokompressor.ru --days 90
    # export GSC cannibalization to a file
    python scripts/cannibalization.py --domain prokompressor.ru --dump cann.csv
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.db.base import SessionLocal, init_db  # noqa: E402
from app.db.models import Site, Source  # noqa: E402
from app.providers.base import DateRange  # noqa: E402
from app.services import cannibalization as C  # noqa: E402
from app.utils import domain_of  # noqa: E402


def _gsc_ids(db, domain: str | None) -> list[int]:
    rows = db.execute(
        select(Site.id, Site.property_uri, Source.code).join(Source, Site.source_id == Source.id)
    ).all()
    return [sid for sid, uri, code in rows
            if code == "gsc" and (not domain or domain_of(uri) == domain)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", help="домен (пусто = все GSC-сайты)")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--min-query-impr", dest="mqi", type=int, default=30)
    ap.add_argument("--min-page-impr", dest="mpi", type=int, default=10)
    ap.add_argument("--max-position", dest="mpos", type=float, default=None)
    ap.add_argument("--keep-home", action="store_true", help="не исключать главную")
    ap.add_argument("--dump", help="выгрузить в CSV/XLSX и выйти")
    a = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        ids = _gsc_ids(db, a.domain or None)
        if not ids:
            print("Нет GSC-сайтов под условие.")
            return
        end = date.today() - timedelta(days=1)
        dr = DateRange(start=end - timedelta(days=a.days - 1), end=end)
        rows = C.gsc_cannibalization(db, ids, dr, min_query_impr=a.mqi, min_page_impr=a.mpi,
                                     max_position=a.mpos, exclude_home=not a.keep_home,
                                     limit=None)
        if a.dump:
            fmt = "xlsx" if a.dump.lower().endswith(".xlsx") else "csv"
            _, buf, _ = C.build_export(rows, a.domain or "all", fmt=fmt)
            with open(a.dump, "wb") as fh:
                fh.write(buf.getvalue())
            print(f"Каннибализированных запросов: {len(rows)} → {a.dump}")
            return
        print(f"Каннибализированных запросов: {len(rows)} (домен {a.domain or 'все'}, {a.days} дн.)")
        for r in rows[:40]:
            ban = " ⚑нестаб" if r["instability"] > 1 else ""
            print(f"  {r['query'][:50]:<50} стр={r['pages_n']} показы={r['total_impr']} "
                  f"топ10={r['top10_pages']}{ban}")
            print(f"      осн: #{r['primary']['position']} {r['primary']['url']}")
            for c in r["cannibals"][:4]:
                print(f"      кан: #{c['position']} {c['url']} ({c['clicks']}кл)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
