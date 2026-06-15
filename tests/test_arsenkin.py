"""ARSENKIN check-top client: result parsing, se config, and the store run."""
from __future__ import annotations

from sqlalchemy import func, select

import app.providers.arsenkin as ARS
import scripts.arsenkin_top as A
from app.db.base import SessionLocal
from app.db.models import SerpResult


def _payload(queries, ss, collect):
    return {"code": "TASK_RESULT", "task_id": "1",
            "result": {"request": {"queries": queries, "ss": ss, "depth": 10},
                       "result": {"collect": collect, "snippets": {}}}}


def test_parse_result_maps_positions_per_query_and_se():
    p = _payload(
        ["ремонт киа", "сервис киа"],
        [{"ss": 2, "region": 213}, {"ss": 11, "region": 1011969}],
        [
            [["https://a.ru/", "https://b.ru/"], ["https://g1.ru/"]],   # query 0: Yandex, Google
            [["https://c.ru/"], ["https://g2.ru/", "https://g3.ru/"]],  # query 1
        ],
    )
    rows = list(ARS.parse_result(p))
    assert ARS.is_done(p)
    # query0 Yandex: pos1 a.ru, pos2 b.ru
    yq0 = [r for r in rows if r["query"] == "ремонт киа" and r["se"] == 2]
    assert [(r["position"], r["url"]) for r in yq0] == [(1, "https://a.ru/"), (2, "https://b.ru/")]
    # query1 Google: two results
    gq1 = [r for r in rows if r["query"] == "сервис киа" and r["se"] == 11]
    assert [(r["position"], r["url"]) for r in gq1] == [(1, "https://g2.ru/"), (2, "https://g3.ru/")]
    assert {r["region"] for r in rows if r["se"] == 2} == {213}


def test_parse_se_config():
    assert A._parse_se("2:213,11:1011969") == [
        {"type": 2, "region": 213}, {"type": 11, "region": 1011969}]
    assert A._parse_se(None) == [
        {"type": 2, "region": 213}, {"type": 11, "region": 1011969}]
    assert A._parse_se("3") == [{"type": 3, "region": 213}]  # default region for Яндекс mobile


class _Resp:
    def __init__(self, data, code=200):
        self._d, self.status_code, self.text = data, code, ""

    def json(self):
        return self._d


class _FakeHTTP:
    """set -> task_id; get -> a TASK_RESULT payload (1 query x 1 se x 2 urls)."""

    def __init__(self):
        self.nid = 0

    def post(self, url, json=None, headers=None, timeout=None):
        if url.endswith("/set"):
            self.nid += 1
            return _Resp({"task_id": self.nid})
        if url.endswith("/get"):
            q = (json or {}).get("task_id")
            return _Resp(_payload(["k1"], [{"ss": 2, "region": 213}],
                                  [[["https://shop.ru/p", "https://rival.ru/x"]]]))
        return _Resp({})


def test_run_stores_serp(db, monkeypatch):
    monkeypatch.setattr(ARS, "httpx", _FakeHTTP())
    monkeypatch.setattr(A.time, "sleep", lambda *_: None)

    A.run(["k1"], [{"type": 2, "region": 213}], token="t",
          base="https://x/api/tools", depth=10, snippets=False, batch=100,
          parallel=5, poll_sec=0, timeout_min=999, max_per_min=999)

    s = SessionLocal()
    try:
        rows = s.execute(select(SerpResult).order_by(SerpResult.position)).scalars().all()
        assert len(rows) == 2
        assert rows[0].keyword == "k1" and rows[0].se == 2 and rows[0].region == 213
        assert rows[0].position == 1 and rows[0].url == "https://shop.ru/p"
        assert rows[0].url_domain == "shop.ru"
        assert s.execute(select(func.count()).select_from(SerpResult)).scalar_one() == 2
    finally:
        s.close()
