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
    """set -> task_id; check -> Done; get -> TASK_RESULT (1 query x 1 se x 2 urls)."""

    def __init__(self):
        self.nid = 0

    def post(self, url, json=None, headers=None, timeout=None):
        if url.endswith("/set"):
            self.nid += 1
            return _Resp({"code": "SET_TASK_OK", "task_id": self.nid, "cost": 2})
        if url.endswith("/check"):
            return _Resp({"status": "Done", "progress": 100})
        if url.endswith("/get"):
            return _Resp(_payload(["k1"], [{"ss": 2, "region": 213}],
                                  [[["https://shop.ru/p", "https://rival.ru/x"]]]))
        return _Resp({})


def test_run_stores_serp(db, monkeypatch):
    import app.services.serp_run as SR
    monkeypatch.setattr(ARS, "httpx", _FakeHTTP())
    monkeypatch.setattr(SR.time, "sleep", lambda *_: None)

    SR.run_top10(["k1"], [{"type": 2, "region": 213}], token="t",
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


def test_dump_writes_csv(db, tmp_path):
    from datetime import date

    from app.db.models import SerpResult
    db.add_all([
        SerpResult(keyword="k1", se=2, region=213, position=1, url="https://shop.ru/p",
                   url_domain="shop.ru", title="T", captured_on=date(2026, 6, 1)),
        SerpResult(keyword="k1", se=2, region=213, position=2, url="https://rival.ru/x",
                   url_domain="rival.ru", captured_on=date(2026, 6, 1)),
    ])
    db.commit()
    out = tmp_path / "serp.csv"
    A.dump(str(out))
    body = out.read_text(encoding="utf-8-sig")
    assert "Запрос" in body and "k1" in body and "shop.ru" in body and "Яндекс" in body
    # only-domain filter keeps just your rows
    out2 = tmp_path / "mine.csv"
    A.dump(str(out2), only_domain="shop.ru")
    b2 = out2.read_text(encoding="utf-8-sig")
    assert "shop.ru" in b2 and "rival.ru" not in b2


def test_check_done_tolerant():
    assert ARS.check_done({"status": "Done", "progress": 100})
    assert ARS.check_done({"progress": "100%"})
    assert ARS.check_done({"code": "TASK_DONE"})
    assert not ARS.check_done({"status": "processing", "progress": 40})
    assert not ARS.check_done({}) and not ARS.check_done(None)


def test_parse_result_snippets():
    p = {"code": "TASK_RESULT", "result": {
        "request": {"queries": ["k"], "ss": [{"ss": 2, "region": 213}], "depth": 10},
        "result": {"collect": [[["https://a.ru/"]]],
                   "snippets": {"https://a.ru/": [{"title": "T", "snippet": "S"}]}}}}
    r = list(ARS.parse_result(p))[0]
    assert r["title"] == "T" and r["snippet"] == "S"


def test_own_positions_summary(db):
    from datetime import date

    from app.db.models import SerpResult
    from app.services.serp import own_positions
    d = date(2026, 6, 1)
    db.add_all([
        SerpResult(keyword="k1", se=2, region=213, position=2, url="https://shop.ru/a",
                   url_domain="shop.ru", captured_on=d),
        SerpResult(keyword="k2", se=2, region=213, position=8, url="https://shop.ru/b",
                   url_domain="shop.ru", captured_on=d),
        SerpResult(keyword="k1", se=2, region=213, position=1, url="https://rival.ru/x",
                   url_domain="rival.ru", captured_on=d),
    ])
    db.commit()
    summ = own_positions(db, d.isoformat(), {"shop.ru"})
    assert len(summ) == 1
    row = summ[0]
    assert row["domain"] == "shop.ru" and row["keywords"] == 2
    assert row["top3"] == 1 and row["top10"] == 2 and row["avg"] == 5.0
