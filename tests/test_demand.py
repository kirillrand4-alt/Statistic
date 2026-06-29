"""Спрос: чтение собранной истории Wordstat (wordstat_history)."""
from __future__ import annotations

import datetime as dt

from app.db.base import SessionLocal
from app.db.models import WordstatHistory
from app.services import demand
from app.utils import query_hash


def _row(q, region, device, y, m, v):
    return WordstatHistory(query=q, query_hash=query_hash(q), region=region,
                           device=device, date=dt.date(y, m, 1), value=v)


def _setup():
    db = SessionLocal()
    db.add_all([
        _row("компрессор купить", "all", "all", 2025, 1, 100),
        _row("компрессор купить", "all", "all", 2025, 2, 150),
        _row("компрессор купить", "all", "all", 2025, 3, 200),
        _row("ресивер 500л", "all", "all", 2025, 1, 40),
        _row("ресивер 500л", "all", "all", 2025, 3, 30),  # gap in 2025-02
        _row("компрессор купить", "msk", "all", 2025, 1, 9),  # other region
    ])
    db.commit()
    return db


def test_regions_devices_and_bounds():
    db = _setup()
    try:
        assert demand.regions(db) == ["all", "msk"]
        assert demand.devices(db) == ["all"]
        lo, hi = demand.bounds(db, "all", "all")
        assert (lo, hi) == (dt.date(2025, 1, 1), dt.date(2025, 3, 1))
    finally:
        db.close()


def test_load_stats_and_series_alignment():
    db = _setup()
    try:
        months, phrases = demand.load(db, "all", "all", None, None)
        assert months == ["2025-01-01", "2025-02-01", "2025-03-01"]
        # sorted by max desc -> компрессор first
        by_q = {p["query"]: p for p in phrases}
        k = by_q["компрессор купить"]
        assert k["points"] == 3
        assert k["min"] == 100 and k["max"] == 200 and k["avg"] == 150
        assert k["first"] == 100 and k["last"] == 200
        assert k["change"] == 100  # 100 -> 200 = +100%
        assert k["series"] == [100, 150, 200]
        # ресивер has a gap in Feb -> None aligned to months
        r = by_q["ресивер 500л"]
        assert r["series"] == [40, None, 30]
        assert r["change"] == -25  # 40 -> 30
    finally:
        db.close()


def test_region_and_search_filters():
    db = _setup()
    try:
        # msk region only has the one phrase point
        _, phrases = demand.load(db, "msk", "all", None, None)
        assert len(phrases) == 1 and phrases[0]["max"] == 9
        # period filter excludes the first month
        months, _ = demand.load(db, "all", "all", dt.date(2025, 2, 1), None)
        assert months == ["2025-02-01", "2025-03-01"]
        # substring search
        _, only = demand.load(db, "all", "all", None, None, "ресивер")
        assert [p["query"] for p in only] == ["ресивер 500л"]
    finally:
        db.close()
