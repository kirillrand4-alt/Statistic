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


def test_error_non_json_and_bad_status_and_structure():
    assert ws._parse_graph(_Resp("<html>not json</html>"))[0] == "error"
    k, msg = ws._parse_graph(_Resp("{}", status=500))
    assert k == "error" and "500" in msg
    # valid JSON but unexpected shape -> error with a reason
    k, msg = ws._parse_graph(_Resp({"graph": {"images": {}}}))
    assert k in ("error", "empty")  # missing timeSeries -> structured failure or empty
