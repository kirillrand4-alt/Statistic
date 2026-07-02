"""Miralinks catalog client: request templating, rowData mapping, paginated run."""
from __future__ import annotations

import re

from sqlalchemy import func, select

import app.providers.miralinks as M
import app.services.donors as D
from app.db.base import SessionLocal
from app.db.models import DonorSite


def _rd(gid, domain, sqi, dr, price, region, topics="СМИ", traffic=1000):
    return {
        "Ground.id": gid, "topDomain": domain, "Ground.folder_url_wl": domain,
        "Ground.name": f"site {gid}", "Ground.description": "desc",
        "Ground.sqi": sqi, "Ground.cy": 100, "Ground.da": 50, "Ground.ahrefs_dr": dr,
        "Ground.cf": 30, "Ground.tf": 25, "Ground.spamness": 0.02,
        "Ground.indexed_percent": 100, "Ground.ya_indexed_count": 39000,
        "Ground.google_indexed_count": 65000, "Ground.traffic": traffic,
        "traffic.interval": "15k-20k", "Ground.ahrefs_traffic": 55000.5,
        "Ground.ahrefs_domains": 5000, "Ground.ahrefs_keywords": 60000,
        "Ground.price_rur": price, "Ground.price_usd": 76.49,
        "Ground.article_price_rur": 430, "Ground.region_id": 9963, "Region.title": region,
        "subj": topics, "langCode": '[["ru","ru"]]', "Ground.links_in_articles": 3,
        "Ground.articles_count": 1102, "ground_user_assessment_avg": 4.9,
        "Ground.placement_time": 119, "Ground.last_placement": "2026-06-12 11:55:45",
        "Ground.venality_int": 0, "Ground.is_exclusive": 0, "isFast": True,
        "Ground.is_pr": 0, "trusted": 1, "screenShot": "https://x/s.jpeg",
    }


def _payload(rows, total):
    return {"sEcho": "7", "iTotalRecords": total, "iTotalDisplayRecords": total,
            "aaData": [{"DT_RowId": r["Ground.id"], "rowData": r} for r in rows]}


def test_set_param_swaps_pagination():
    body = "sEcho=7&iDisplayStart=20&iDisplayLength=20&x=1"
    out = M.set_param(M.set_param(body, "iDisplayStart", 100), "iDisplayLength", 50)
    assert "iDisplayStart=100" in out and "iDisplayLength=50" in out
    assert "iDisplayStart=20" not in out
    # appends when missing
    assert "foo=9" in M.set_param("a=1", "foo", 9)


def test_map_row_extracts_clean_fields():
    r = M.map_row(_rd(136945, "stolica-s.su", 5550, 52, 5500, "Республика Мордовия"))
    assert r["external_id"] == "136945" and r["domain"] == "stolica-s.su"
    assert r["sqi"] == 5550 and r["ahrefs_dr"] == 52 and r["price_rur"] == 5500
    assert r["region"] == "Республика Мордовия" and r["lang"] == "ru"
    assert r["is_fast"] == 1 and r["traffic"] == 1000
    assert '"Ground.id": 136945' in r["raw"]


def test_total_records_and_parse():
    p = _payload([_rd(1, "a.ru", 5000, 40, 800, "Россия")], 3410)
    assert M.total_records(p) == 3410
    rows = list(M.parse_rows(p))
    assert len(rows) == 1 and rows[0]["domain"] == "a.ru"


def test_auth_failed_detection():
    assert M._auth_failed(302, "")
    assert M._auth_failed(200, "<!DOCTYPE html><html>login</html>")
    assert not M._auth_failed(200, '{"aaData": []}')


class _Resp:
    def __init__(self, data=None, code=200, text='{"ok":1}'):
        self._d, self.status_code, self.text = data, code, text

    def json(self):
        if self._d is None:
            raise ValueError("no json")
        return self._d


class _FakeHTTP:
    """Two pages (2 rows, then 1 row), then empty; total=3."""

    def post(self, url, content=None, headers=None, timeout=None, follow_redirects=None):
        body = content.decode() if isinstance(content, bytes) else (content or "")
        start = int(re.search(r"iDisplayStart=(\d+)", body).group(1))
        if start == 0:
            return _Resp(_payload([_rd(1, "a.ru", 5000, 40, 800, "Россия"),
                                   _rd(2, "b.ru", 4000, 50, 300, "Москва")], 3))
        if start == 2:
            return _Resp(_payload([_rd(3, "c.ru", 3000, 30, 1500, "Россия")], 3))
        return _Resp(_payload([], 3))


def test_run_catalog_stores_and_upserts(db, monkeypatch):
    monkeypatch.setattr(M, "httpx", _FakeHTTP())
    res = D.run_catalog("cookie", "iDisplayStart=0&iDisplayLength=100", length=100, pause=0)
    assert res["fetched"] == 3 and res["stored"] == 3 and res["error"] is None

    s = SessionLocal()
    try:
        assert s.execute(select(func.count()).select_from(DonorSite)).scalar_one() == 3
        a = s.execute(select(DonorSite).where(DonorSite.domain == "a.ru")).scalar_one()
        assert a.sqi == 5000 and a.source == "miralinks"
    finally:
        s.close()

    # re-run upserts (no duplicates)
    D.run_catalog("cookie", "iDisplayStart=0&iDisplayLength=100", length=100, pause=0)
    s = SessionLocal()
    try:
        assert s.execute(select(func.count()).select_from(DonorSite)).scalar_one() == 3
    finally:
        s.close()


def test_run_catalog_reports_auth_error(db, monkeypatch):
    class _Auth:
        def post(self, *a, **k):
            return _Resp(None, code=403, text="<html>login</html>")

    monkeypatch.setattr(M, "httpx", _Auth())
    res = D.run_catalog("bad", "iDisplayStart=0&iDisplayLength=100", pause=0)
    assert res["fetched"] == 0 and res["error"]


def test_donor_rows_filter_and_export(db):
    from datetime import date
    db.add_all([
        DonorSite(source="miralinks", external_id="1", domain="a.ru", sqi=5000,
                  ahrefs_dr=40, price_rur=800, traffic=20000, region="Россия",
                  topics="СМИ", captured_on=date(2026, 6, 1)),
        DonorSite(source="miralinks", external_id="2", domain="b.ru", sqi=3000,
                  ahrefs_dr=60, price_rur=300, traffic=500, region="Москва",
                  topics="Бизнес", captured_on=date(2026, 6, 1)),
    ])
    db.commit()
    assert D.count(db) == 2
    assert "Россия" in D.regions(db) and "Москва" in D.regions(db)
    # min_sqi keeps only a.ru
    rows = D.donor_rows(db, min_sqi=4000)
    assert [r.domain for r in rows] == ["a.ru"]
    # region filter
    assert [r.domain for r in D.donor_rows(db, region="Москва")] == ["b.ru"]
    # price sort ascending
    assert [r.domain for r in D.donor_rows(db, sort="price")] == ["b.ru", "a.ru"]

    fname, buf, _ = D.build_export(D.donor_rows(db), fmt="csv")
    body = buf.getvalue().decode("utf-8-sig")
    assert fname.endswith(".csv") and "Домен" in body and "a.ru" in body


def test_search_data_roundtrip_and_patch():
    body = ("sEcho=7&iColumns=41&iDisplayStart=0&iDisplayLength=20"
            "&searchData=%7B%22catalog%22%3A%22yandex%22%2C%22sqiFrom%22%3A100%7D&bar=1")
    sd = M.get_search_data(body)
    assert sd == {"catalog": "yandex", "sqiFrom": 100}
    # patch: numbers coerced, string kept, missing key added, None removes
    out = M.patch_search_data(body, {"sqiFrom": "3860", "sqiTo": "5550",
                                     "mrFrom": "100", "mrTo": "100", "catalog": None})
    sd2 = M.get_search_data(out)
    assert sd2 == {"sqiFrom": 3860, "sqiTo": 5550, "mrFrom": 100, "mrTo": 100}
    # untouched params survive and pagination still swappable
    assert "iColumns=41" in out and "bar=1" in out
    assert "iDisplayStart=99" in M.set_param(out, "iDisplayStart", 99)


def test_get_search_data_absent_or_bad():
    assert M.get_search_data("sEcho=7&iDisplayStart=0") == {}
    assert M.get_search_data("searchData=not-json") == {}
    # appends searchData when the body has none
    out = M.set_search_data("sEcho=7", {"sqiFrom": 3860})
    assert M.get_search_data(out) == {"sqiFrom": 3860}


def test_wipe_clears_all_donors(db):
    from datetime import date
    db.add_all([
        DonorSite(source="miralinks", external_id="1", domain="a.ru", sqi=5000,
                  captured_on=date(2026, 6, 1)),
        DonorSite(source="miralinks", external_id="2", domain="b.ru", sqi=3000,
                  captured_on=date(2026, 6, 1)),
        DonorSite(source="other", external_id="9", domain="c.ru", sqi=1000,
                  captured_on=date(2026, 6, 1)),
    ])
    db.commit()
    assert D.count(db) == 3
    # default wipes only the miralinks source
    assert D.wipe(db) == 2
    assert D.count(db) == 1
    assert [r.domain for r in D.donor_rows(db)] == ["c.ru"]
    # source=None wipes everything
    assert D.wipe(db, source=None) == 1
    assert D.count(db) == 0
