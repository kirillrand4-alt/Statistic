"""Capture an indexed-URL snapshot for every enabled site that supports it.

    python scripts/snapshot_index.py

Iterates enabled sites whose source exposes the index list (Yandex) and stores
today's snapshot for each, so the "Индексация" page can compare over time.
Run detached for many sites:
    nohup .venv/bin/python scripts/snapshot_index.py > /tmp/snap.log 2>&1 &
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.db.base import SessionLocal, init_db  # noqa: E402
from app.db.models import Site  # noqa: E402
from app.services import indexing  # noqa: E402


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        sites = db.execute(select(Site).where(Site.enabled.is_(True)).order_by(Site.id)).scalars().all()
        targets = [s for s in sites if indexing.supports(s)]
        if not targets:
            print("No enabled sites support index snapshots (connect Yandex first).")
            return
        print(f"Snapshotting {len(targets)} site(s)...")
        for i, s in enumerate(targets, 1):
            print(f"[{i}/{len(targets)}] {s.property_uri} ...")
            try:
                n = indexing.capture_indexed_urls(db, s)
                print(f"    ok, {n} URLs")
            except Exception as exc:  # noqa: BLE001
                print(f"    ERROR: {exc}")
        print("Done.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
