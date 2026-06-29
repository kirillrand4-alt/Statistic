"""scripts/wordstat.py resume helpers: --skip-done must skip already-collected phrases."""
from __future__ import annotations

import datetime as dt
import importlib.util
import os

from app.db.base import SessionLocal
from app.db.models import WordstatHistory
from app.utils import query_hash

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("wordstat", os.path.join(_REPO, "scripts", "wordstat.py"))
ws = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws)


def test_month_floor():
    assert ws._month_floor("30.06.2026") == dt.date(2026, 6, 1)
    assert ws._month_floor("01.01.2024") == dt.date(2024, 1, 1)


def test_done_hashes_scopes_region_device_period():
    db = SessionLocal()
    try:
        h_done = query_hash("компрессор купить")
        db.add_all([
            # collected for all/all within window -> done
            WordstatHistory(query="компрессор купить", query_hash=h_done, region="all",
                            device="all", date=dt.date(2025, 3, 1), value=100),
            # same phrase but different region -> NOT done for all/all
            WordstatHistory(query="ресивер", query_hash=query_hash("ресивер"), region="msk",
                            device="all", date=dt.date(2025, 3, 1), value=10),
            # outside the window -> NOT counted
            WordstatHistory(query="старое", query_hash=query_hash("старое"), region="all",
                            device="all", date=dt.date(2020, 1, 1), value=5),
        ])
        db.commit()
        done = ws._done_hashes(db, "all", "all", dt.date(2025, 1, 1), dt.date(2025, 12, 1))
        assert h_done in done
        assert query_hash("ресивер") not in done   # wrong region
        assert query_hash("старое") not in done     # outside period

        # filtering mirrors cmd_collect's skip step
        phrases = ["компрессор купить", "новая фраза"]
        remaining = [p for p in phrases if query_hash(p) not in done]
        assert remaining == ["новая фраза"]
    finally:
        db.close()
