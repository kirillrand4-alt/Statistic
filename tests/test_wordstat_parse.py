"""scripts/wordstat.py _parse_graph: ok / empty / captcha / error classification."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("wordstat", os.path.join(_REPO, "scripts", "wordstat.py"))
ws = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws)


class _Resp:
    def __init__(self, body, status=200):
        self._body = body if isinstance(body, str) else json.dumps(body)
        self.status = status

    def text(self):
        return self._body


def _graph(absolute):
    return {"graph": {"images": {"timeSeries": {"preparedValues": {"absolute": absolute}}}}}


def test_ok_series():
    body = _graph([{"year": 2025, "month": 0, "y": 100}, {"year": 2025, "month": 1, "y": 150}])
    kind, rows = ws._parse_graph(_Resp(body))
    assert kind == "ok"
    assert rows == [(dt.date(2025, 1, 1), 100), (dt.date(2025, 2, 1), 150)]


def test_empty_when_no_absolute_series():
    assert ws._parse_graph(_Resp({"graph": {"images": {"timeSeries": {"preparedValues": {}}}}})) == ("empty", [])
    assert ws._parse_graph(_Resp(_graph([])))[0] == "empty"


def test_captcha_detected_by_marker_and_by_status():
    assert ws._parse_graph(_Resp("<html>showCaptcha…</html>"))[0] == "captcha"
    assert ws._parse_graph(_Resp("{}", status=429))[0] == "captcha"
    assert ws._parse_graph(_Resp("{}", status=403))[0] == "captcha"


def test_daily_points_parsed_with_iso_day_string():
    # real Wordstat day format: day is an ISO date string, month/year are null
    body = {"graph": {"images": {"timeSeries": {"preparedValues": {"absolute": [
        {"month": None, "day": "2026-05-02", "y": 60},
        {"month": None, "day": "2026-06-01", "y": 153},
        {"month": None, "day": "2026-06-29", "y": 189},
    ]}}}}}
    kind, rows = ws._parse_graph(_Resp(body))
    assert kind == "ok"
    assert rows == [(dt.date(2026, 5, 2), 60), (dt.date(2026, 6, 1), 153),
                    (dt.date(2026, 6, 29), 189)]


def test_daily_skips_null_value_points():
    # day series may carry null y for no-data days — must not crash, just skip
    body = {"graph": {"images": {"timeSeries": {"preparedValues": {"absolute": [
        {"month": None, "day": "2026-05-30", "y": None},        # gap -> skipped
        {"month": None, "day": "2026-06-01", "y": 153},
        {"month": None, "day": "2026-06-02", "value": 148},     # alt key 'value'
        {"month": None, "day": "not-a-date", "y": 5},           # bad date -> skipped
    ]}}}}}
    kind, rows = ws._parse_graph(_Resp(body))
    assert kind == "ok"
    assert rows == [(dt.date(2026, 6, 1), 153), (dt.date(2026, 6, 2), 148)]


def test_monthly_points_still_day_1():
    body = _graph([{"year": 2025, "month": 0, "y": 100}])  # no 'day' -> defaults to 1
    _, rows = ws._parse_graph(_Resp(body))
    assert rows == [(dt.date(2025, 1, 1), 100)]


def test_save_routes_day_to_series_table():
    from app.db.base import SessionLocal
    from app.db.models import WordstatHistory, WordstatSeries
    from app.utils import query_hash
    db = SessionLocal()
    try:
        rows = [(dt.date(2026, 6, 1), 1993), (dt.date(2026, 6, 2), 2018)]
        n = ws._save(db, "винтовой компрессор", "all", "desktop,phone,tablet", rows, graph="day")
        assert n == 2
        got = db.query(WordstatSeries).all()
        assert len(got) == 2
        assert all(r.granularity == "day" and r.device == "all" for r in got)
        assert {r.date for r in got} == {dt.date(2026, 6, 1), dt.date(2026, 6, 2)}
        # monthly table untouched
        assert db.query(WordstatHistory).count() == 0
        # re-save upserts (no duplicates)
        ws._save(db, "винтовой компрессор", "all", "desktop,phone,tablet",
                 [(dt.date(2026, 6, 1), 2000)], graph="day")
        db.expire_all()  # core upsert bypassed the identity map — refetch from DB
        assert db.query(WordstatSeries).count() == 2
        v = db.query(WordstatSeries).filter_by(date=dt.date(2026, 6, 1)).one().value
        assert v == 2000
    finally:
        db.close()


def test_sanitize_phrase():
    # slash / colon / semicolon / backslash → space; word order & operators kept
    assert ws._sanitize_phrase("компрессор 1000 л/мин") == "компрессор 1000 л мин"
    assert ws._sanitize_phrase("тз: быстро; дёшево") == "тз быстро дёшево"
    assert ws._sanitize_phrase("компрессор винтовой") == "компрессор винтовой"  # unchanged
    assert ws._sanitize_phrase("a  /  b") == "a b"  # collapse extra spaces


def test_error_non_json_and_bad_status_and_structure():
    assert ws._parse_graph(_Resp("<html>not json</html>"))[0] == "error"
    k, msg = ws._parse_graph(_Resp("{}", status=500))
    assert k == "error" and "500" in msg
    # valid JSON but unexpected shape -> error with a reason
    k, msg = ws._parse_graph(_Resp({"graph": {"images": {}}}))
    assert k in ("error", "empty")  # missing timeSeries -> structured failure or empty
