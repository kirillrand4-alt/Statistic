"""Metrica hits/visits import (extra + upsert), counter resolution and the
rotated multi-domain downloader (scripts/metrika_logs.sync_rotate) with a mocked
Logs API."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

import scripts.metrika_logs as M
from app.bootstrap import ensure_sources
from app.db.base import SessionLocal
from app.db.models import Hit, Site, Visit
from app.services import visits as V

VHDR = "ym:s:visitID\tym:s:date\tym:s:deviceCategory\tym:s:pageViews\tym:s:lastUTMSource"
HHDR = "ym:pv:watchID\tym:pv:date\tym:pv:URL\tym:pv:deviceCategory\tym:pv:isPageView"


def _mksite(db, host: str) -> int:
    src = ensure_sources(db)["yandex_webmaster"]
    s = Site(source_id=src.id, property_uri=f"https://{host}/", display_name=host)
    db.add(s)
    db.commit()
    return s.id


def _lines(*rows: str):
    return iter(rows)


def test_import_hits_and_extra(db):
    sid = _mksite(db, "a.ru")
    n = V.import_hits(db, sid, _lines(HHDR, "10\t2026-06-01\thttp://a.ru/x\tdesktop\t1"))
    assert n == 1
    row = db.execute(select(Hit).where(Hit.watch_id == 10)).scalar_one()
    assert row.url == "http://a.ru/x" and row.device == "desktop" and row.is_page_view == 1


def test_visit_extra_and_upsert(db):
    sid = _mksite(db, "a.ru")
    # unmapped ym:s:lastUTMSource is preserved in the JSON extra column
    V.import_tsv(db, sid, _lines(VHDR, "100\t2026-06-01\tdesktop\t2\tgoogle"))
    assert '"lastUTMSource": "google"' in db.execute(
        select(Visit.extra).where(Visit.visit_id == 100)
    ).scalar_one()

    # update=True overwrites existing row (no duplicate)
    V.import_tsv(db, sid, _lines(VHDR, "100\t2026-06-01\tmobile\t9\tyandex"), update=True)
    db.expire_all()
    dev, pv = db.execute(
        select(Visit.device, Visit.page_views).where(Visit.visit_id == 100)
    ).one()
    assert dev == "mobile" and pv == 9
    assert db.execute(select(func.count()).where(Visit.visit_id == 100)).scalar_one() == 1

    # update=False keeps the existing row untouched
    V.import_tsv(db, sid, _lines(VHDR, "100\t2026-06-01\tdesktop\t1\tbing"))
    db.expire_all()
    assert db.execute(select(Visit.device).where(Visit.visit_id == 100)).scalar_one() == "mobile"


def test_import_handles_huge_ids(db):
    """A Metrica id past the signed-64-bit column limit is skipped (not a crash),
    while the max valid id is stored EXACTLY (parsing must not round via float)."""
    sid = _mksite(db, "big.ru")
    maxok = 2**63 - 1          # fits exactly — int(float(id)) would round this up
    huge = 2**63 + 100         # past the limit — row skipped, no OverflowError
    n = V.import_tsv(db, sid, _lines(
        VHDR,
        f"{maxok}\t2026-06-01\tdesktop\t1\tg",
        f"{huge}\t2026-06-01\tmobile\t2\ty",
    ))
    assert n == 1
    ids = {v for (v,) in db.execute(
        select(Visit.visit_id).where(Visit.site_id == sid)).all()}
    assert ids == {maxok}


def test_resolve_targets(db, monkeypatch):
    a, b, c = _mksite(db, "a.ru"), _mksite(db, "b.ru"), _mksite(db, "c.ru")
    # a.ru learns its counter from an existing visit; b/c from the counter list
    db.add(Visit(site_id=a, visit_id=1, counter_id=111, date=date(2026, 6, 1)))
    db.commit()
    monkeypatch.setattr(M, "_fetch_counters", lambda: [
        {"id": 222, "site": "b.ru"}, {"id": 333, "site": "www.c.ru"}, {"id": 9, "site": "x.ru"},
    ])
    targets, missed = M.resolve_targets()
    assert {(lbl, cid) for _, cid, lbl in targets} == {("a.ru", 111), ("b.ru", 222), ("c.ru", 333)}
    assert missed == []


def test_resolve_targets_merges_domain_twins(db, monkeypatch):
    """sc-domain: + https:// of one domain collapse into ONE download target —
    the twin that already holds the visits."""
    src = ensure_sources(db)["yandex_webmaster"]
    a = Site(source_id=src.id, property_uri="sc-domain:d.ru", display_name="sc:d.ru")
    b = Site(source_id=src.id, property_uri="https://d.ru/", display_name="d.ru")
    db.add_all([a, b])
    db.commit()
    db.add(Visit(site_id=b.id, visit_id=1, counter_id=999, date=date(2026, 6, 1)))
    db.commit()
    monkeypatch.setattr(M, "_fetch_counters", lambda: [])

    targets, missed = M.resolve_targets()
    assert targets == [(b.id, 999, "d.ru")] and missed == []


def test_merge_domain_dupes_consolidates(db):
    """Rows already downloaded under a duplicate site move onto the canonical
    one; overlapping ids are kept once, the dup site ends up empty."""
    src = ensure_sources(db)["yandex_webmaster"]
    a = Site(source_id=src.id, property_uri="sc-domain:m.ru", display_name="sc:m.ru")
    b = Site(source_id=src.id, property_uri="https://m.ru/", display_name="m.ru")
    db.add_all([a, b])
    db.commit()
    db.add_all([
        Visit(site_id=b.id, visit_id=1, date=date(2026, 6, 1)),
        Visit(site_id=b.id, visit_id=2, date=date(2026, 6, 1)),
        Visit(site_id=b.id, visit_id=4, date=date(2026, 6, 2)),
        Visit(site_id=a.id, visit_id=2, date=date(2026, 6, 1)),   # overlap — kept once
        Visit(site_id=a.id, visit_id=3, date=date(2026, 6, 2)),   # unique — moved
        Hit(site_id=a.id, watch_id=7, date=date(2026, 6, 1), url="http://m.ru/x"),
    ])
    db.commit()

    moved = M.merge_domain_dupes()

    assert moved == 3  # 2 visits + 1 hit removed from the dup site
    s = SessionLocal()
    try:
        vis = {v for (v,) in s.execute(
            select(Visit.visit_id).where(Visit.site_id == b.id)).all()}
        assert vis == {1, 2, 3, 4}
        assert s.execute(select(func.count()).where(Visit.site_id == a.id)).scalar_one() == 0
        assert s.execute(select(Hit.site_id).where(Hit.watch_id == 7)).scalar_one() == b.id
    finally:
        s.close()


# --- mocked Logs API for sync_rotate ----------------------------------------
class _Resp:
    def __init__(self, data, code=200):
        self._d, self.status_code, self.text = data, code, ""

    def json(self):
        return self._d


class _Stream:
    def __init__(self, text):
        self._t, self.status_code = text, 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_text(self):
        yield self._t


def _counter(url):
    return int(url.split("/counter/")[1].split("/")[0])


def _rid(url):
    return int(url.split("/logrequest/")[1].split("/")[0])


class FakeHTTP:
    """Minimal stand-in for the Logs API: create -> status -> download -> clean."""

    def __init__(self, status="processed", eval_days=None, counters=None):
        self.status = status
        self.eval_days = eval_days       # what /evaluate reports
        self.counters = counters or []   # what /counters reports (for create dates)
        self.reqs: dict[int, dict] = {}
        self.nid = 0
        self.created, self.cleaned, self.cancelled = [], [], []
        self.max_open = 0

    def post(self, url, headers=None, params=None, timeout=None):
        if url.endswith("/logrequests"):
            self.nid += 1
            self.reqs[self.nid] = {"source": params["source"], "date1": params["date1"],
                                   "date2": params["date2"]}
            self.created.append((self.nid, _counter(url), params["source"]))
            self.max_open = max(self.max_open, len(self.reqs) - len(self.cleaned) - len(self.cancelled))
            return _Resp({"log_request": {"request_id": self.nid}})
        if url.endswith("/clean"):
            self.cleaned.append(_rid(url))
        elif url.endswith("/cancel"):
            self.cancelled.append(_rid(url))
        return _Resp({})

    def get(self, url, headers=None, params=None, timeout=None):
        if url.endswith("/counters"):
            return _Resp({"counters": self.counters})
        if url.endswith("/evaluate"):
            ed = self.eval_days
            if callable(ed):  # size depends on the probed span
                ed = ed(params["date1"], params["date2"])
            return _Resp({"log_request_evaluation": {"max_possible_day_quantity": ed}})
        return _Resp({"log_request": {"status": self.status, "parts": [{"part_number": 0}]}})

    def stream(self, method, url, headers=None, timeout=None):
        r = self.reqs[_rid(url)]
        if r["source"] == "hits":
            t = f"ym:pv:watchID\tym:pv:date\tym:pv:URL\n{_rid(url)*10+1}\t{r['date1']}\thttp://x\n"
        else:
            t = f"ym:s:visitID\tym:s:date\tym:s:deviceCategory\n{_rid(url)*10+1}\t{r['date1']}\tdesktop\n"
        return _Stream(t)


def test_sync_rotate_happy(db, monkeypatch):
    targets = [(_mksite(db, h), cid, h) for h, cid in [("a.ru", 111), ("b.ru", 222), ("c.ru", 333)]]
    fake = FakeHTTP(status="processed")
    monkeypatch.setattr(M, "httpx", fake)
    monkeypatch.setattr(M, "_token", lambda: "t")

    M.sync_rotate(targets, date(2026, 6, 1), date(2026, 6, 6), chunk=3, parallel=3,
                  sources=("visits", "hits"), force=True, timeout_min=999, poll_sec=0)

    # 3 domains x 2 windows x 2 sources = 12 requests, all cleaned, never >3 in flight
    assert len(fake.created) == 12 and len(fake.cleaned) == 12 and not fake.cancelled
    assert fake.max_open <= 3
    assert {c for _, c, _ in fake.created} == {111, 222, 333}
    s = SessionLocal()
    try:
        assert s.execute(select(func.count()).select_from(Visit)).scalar_one() == 6
        assert s.execute(select(func.count()).select_from(Hit)).scalar_one() == 6
    finally:
        s.close()


def test_sync_rotate_newest_first(db, monkeypatch):
    """Windows are anchored to the END of the period and go newest -> oldest;
    parallel defaults to one in-flight request per domain."""
    sid = _mksite(db, "n.ru")
    fake = FakeHTTP(status="processed")
    monkeypatch.setattr(M, "httpx", fake)
    monkeypatch.setattr(M, "_token", lambda: "t")

    M.sync_rotate([(sid, 777, "n.ru")], date(2026, 6, 1), date(2026, 6, 11), chunk=10,
                  sources=("visits",), force=True, timeout_min=999, poll_sec=0)

    windows = [(fake.reqs[r]["date1"], fake.reqs[r]["date2"]) for r, _, _ in fake.created]
    assert windows == [("2026-06-02", "2026-06-11"), ("2026-06-01", "2026-06-01")]


def test_sync_rotate_survives_rate_limit(db, monkeypatch):
    """429 on create or a failure mid-download doesn't abort the run — the
    window is retried on the next poll and still gets imported."""

    class Flaky(FakeHTTP):
        def __init__(self):
            super().__init__("processed")
            self.fail_create = self.fail_stream = True

        def post(self, url, **kw):
            if url.endswith("/logrequests") and self.fail_create:
                self.fail_create = False
                return _Resp({}, code=429)
            return super().post(url, **kw)

        def stream(self, *a, **kw):
            if self.fail_stream:
                self.fail_stream = False
                raise RuntimeError("simulated 429 mid-download")
            return super().stream(*a, **kw)

    sid = _mksite(db, "r.ru")
    fake = Flaky()
    monkeypatch.setattr(M, "httpx", fake)
    monkeypatch.setattr(M, "_token", lambda: "t")

    M.sync_rotate([(sid, 888, "r.ru")], date(2026, 6, 1), date(2026, 6, 3), chunk=3,
                  sources=("visits",), force=True, timeout_min=999, poll_sec=0)

    assert len(fake.created) == 1 and len(fake.cleaned) == 1 and not fake.cancelled
    s = SessionLocal()
    try:
        assert s.execute(select(func.count()).select_from(Visit)).scalar_one() == 1
    finally:
        s.close()


def test_sync_rotate_adaptive_window(db, monkeypatch):
    """With no --chunk the window size comes from the API's evaluate
    (max_possible_day_quantity), and the period is clipped to the counter's
    create date."""
    sid = _mksite(db, "ad.ru")
    fake = FakeHTTP(status="processed", eval_days=4,
                    counters=[{"id": 777, "create_time": "2026-06-05 00:00:00"}])
    monkeypatch.setattr(M, "httpx", fake)
    monkeypatch.setattr(M, "_token", lambda: "t")

    # asked for 06-01..06-11, but counter starts 06-05 and evaluate caps at 4 days
    M.sync_rotate([(sid, 777, "ad.ru")], date(2026, 6, 1), date(2026, 6, 11),
                  chunk=None, max_chunk=30, sources=("visits",), force=True,
                  timeout_min=999, poll_sec=0)

    windows = [(fake.reqs[r]["date1"], fake.reqs[r]["date2"]) for r, _, _ in fake.created]
    assert windows == [("2026-06-08", "2026-06-11"), ("2026-06-05", "2026-06-07")]


def test_sync_rotate_evaluate_capped_by_max_chunk(db, monkeypatch):
    """A huge evaluate result is capped at --max-chunk so one request can't
    cover an unbounded span."""
    sid = _mksite(db, "cap.ru")
    fake = FakeHTTP(status="processed", eval_days=999, counters=[])
    monkeypatch.setattr(M, "httpx", fake)
    monkeypatch.setattr(M, "_token", lambda: "t")

    M.sync_rotate([(sid, 1, "cap.ru")], date(2026, 6, 1), date(2026, 6, 20),
                  chunk=None, max_chunk=7, sources=("visits",), force=True,
                  timeout_min=999, poll_sec=0)

    spans = [(date.fromisoformat(fake.reqs[r]["date2"])
              - date.fromisoformat(fake.reqs[r]["date1"])).days + 1
             for r, _, _ in fake.created]
    assert max(spans) == 7 and len(fake.created) == 3  # 20 days / 7 -> 7+7+6


def test_sync_rotate_evaluate_probes_recent_on_wide_range(db, monkeypatch):
    """A year+ range that evaluate won't size falls back to probing a recent
    ~90-day window instead of dropping to the default 10."""
    sid = _mksite(db, "wide.ru")

    def ev(d1, d2):  # the wide range returns nothing; a <=90-day probe gives 5
        span = (date.fromisoformat(d2) - date.fromisoformat(d1)).days + 1
        return None if span > 90 else 5

    fake = FakeHTTP(status="processed", eval_days=ev, counters=[])
    monkeypatch.setattr(M, "httpx", fake)
    monkeypatch.setattr(M, "_token", lambda: "t")

    M.sync_rotate([(sid, 1, "wide.ru")], date(2026, 1, 1), date(2026, 6, 20),
                  chunk=None, max_chunk=30, sources=("visits",), force=True,
                  timeout_min=999, poll_sec=0)

    spans = [(date.fromisoformat(fake.reqs[r]["date2"])
              - date.fromisoformat(fake.reqs[r]["date1"])).days + 1
             for r, _, _ in fake.created]
    assert spans and max(spans) == 5  # sized from the 90-day probe, not the default


def test_sync_rotate_timeout_skips(db, monkeypatch):
    sid = _mksite(db, "z.ru")
    fake = FakeHTTP(status="created")  # never becomes ready
    monkeypatch.setattr(M, "httpx", fake)
    monkeypatch.setattr(M, "_token", lambda: "t")

    M.sync_rotate([(sid, 555, "z.ru")], date(2026, 6, 1), date(2026, 6, 3), chunk=3,
                  parallel=3, sources=("visits",), force=True, timeout_min=0, poll_sec=0)

    assert len(fake.created) == 1 and len(fake.cancelled) == 1 and not fake.cleaned
    s = SessionLocal()
    try:
        assert s.execute(select(func.count()).select_from(Visit)).scalar_one() == 0
    finally:
        s.close()
