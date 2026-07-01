"""Аудитория / охват: capture-recapture по IP+UA между сайтами."""
from __future__ import annotations

import datetime as dt
import json

from app.bootstrap import ensure_sources
from app.db.base import SessionLocal
from app.db.models import Site, Visit
from app.providers.base import DateRange
from app.services import audience as A


def _visit(sid, vid, ip, src, day=20, ua=("desktop", "Windows", "Chrome"), utm_term="компрессор"):
    extra = {}
    if utm_term is not None:
        extra["lastUTMTerm"] = utm_term
        extra["lastUTMSource"] = "yandex"
    return Visit(site_id=sid, visit_id=vid, date=dt.date(2026, 6, day),
                 ip=ip, device=ua[0], os=ua[1], browser=ua[2], traffic_source=src,
                 extra=json.dumps(extra) if extra else None)


def _mksite(db, uri):
    gsc = ensure_sources(db)["gsc"]
    s = Site(source_id=gsc.id, property_uri=uri, display_name=uri)
    db.add(s)
    db.commit()
    return s.id


def test_chapman_and_schnabel_math():
    # 100 & 120 with 30 overlap -> Petersen ~400, Chapman close
    ch = A.chapman(100, 120, 30)
    assert 380 <= ch["n"] <= 410
    assert ch["lo"] <= ch["n"] <= ch["hi"]
    assert A.chapman(10, 10, 0) is None  # no overlap -> not estimable
    # schnabel: two identical-size sets sharing half
    a = {f"k{i}" for i in range(100)}
    b = {f"k{i}" for i in range(50, 150)}   # overlap 50
    est = A.schnabel([a, b])
    assert est is not None and est >= len(a | b)  # never below observed union


def test_source_and_keyword_filters():
    # ad with keyword -> counts; ad without keyword -> excluded under ad_kw_search
    assert A._source_ok("ad", None, None, '{"lastUTMTerm": "компрессор"}', True, "ad_kw_search")
    assert not A._source_ok("ad", None, None, None, False, "ad_kw_search")   # ad, no kw
    assert A._source_ok("organic", None, "yandex", None, False, "ad_kw_search")  # search always
    assert not A._source_ok("direct", None, None, None, False, "ad_kw_search")   # direct excluded
    assert A._source_ok("direct", None, None, None, False, "all")               # all keeps direct
    # _has_kw
    assert A._has_kw('{"lastUTMTerm": "купить компрессор"}')
    assert not A._has_kw('{"lastUTMTerm": "(not set)"}')
    assert not A._has_kw(None)


def test_estimate_two_sites_overlap():
    db = SessionLocal()
    try:
        a = _mksite(db, "https://a.ru/")
        b = _mksite(db, "https://b.ru/")
        # site A: ips 1..6 ; site B: ips 4..9 ; overlap {4,5,6} = 3, same UA & day
        for n in range(1, 7):
            db.add(_visit(a, f"a{n}", f"10.0.0.{n}", "ad"))
        for n in range(4, 10):
            db.add(_visit(b, f"b{n}", f"10.0.0.{n}", "ad"))
        # a direct/bot visit with a fresh ip must be ignored (no keyword, direct)
        db.add(_visit(a, "bot1", "10.0.0.200", "direct", utm_term=None))
        db.commit()

        dr = DateRange(start=dt.date(2026, 6, 1), end=dt.date(2026, 6, 30))
        r = A.estimate(db, [a, b], dr, source="ad_kw_search")
        assert r["with_ip"] == 12          # bot visit excluded (direct, no kw)
        assert r["period_observed"] == 9   # union of ips 1..9
        assert len(r["pairs"]) == 1
        p = r["pairs"][0]
        assert p["na"] == 6 and p["nb"] == 6 and p["overlap"] == 3
        # Chapman: (7*7/4)-1 = 11.25 -> 11
        assert p["chapman"]["n"] == 11
        assert r["period_estimate"] is not None
        # one day, both sites present -> a daily estimate exists
        assert any(d["estimate"] for d in r["days"])
    finally:
        db.close()


def test_monthly_bucket_groups_by_month():
    db = SessionLocal()
    try:
        a = _mksite(db, "https://a.ru/")
        b = _mksite(db, "https://b.ru/")
        # same visitors on two different days of June -> one MONTH bucket
        for day in (5, 20):
            for n in range(1, 7):
                db.add(_visit(a, f"a{day}{n}", f"10.0.0.{n}", "ad", day=day))
            for n in range(4, 10):
                db.add(_visit(b, f"b{day}{n}", f"10.0.0.{n}", "ad", day=day))
        db.commit()
        dr = DateRange(start=dt.date(2026, 6, 1), end=dt.date(2026, 6, 30))
        by_day = A.estimate(db, [a, b], dr, bucket="day")
        by_month = A.estimate(db, [a, b], dr, bucket="month")
        assert len(by_day["days"]) == 2      # two calendar days
        assert len(by_month["days"]) == 1    # collapsed into one month
        mb = by_month["days"][0]
        assert mb["date"] == "2026-06-01"    # month-floor
        assert mb["observed"] == 9           # union of ips 1..9 over the month
        assert mb["estimate"] is not None
    finally:
        db.close()


def test_ip_ua_distinguishes_shared_ip():
    db = SessionLocal()
    try:
        a = _mksite(db, "https://a.ru/")
        b = _mksite(db, "https://b.ru/")
        # SAME ip on both sites but DIFFERENT device -> not the same person under ip_ua
        db.add(_visit(a, "a1", "5.5.5.5", "ad", ua=("desktop", "Windows", "Chrome")))
        db.add(_visit(b, "b1", "5.5.5.5", "ad", ua=("mobile", "Android", "YandexBrowser")))
        db.commit()
        dr = DateRange(start=dt.date(2026, 6, 1), end=dt.date(2026, 6, 30))
        r_ua = A.estimate(db, [a, b], dr, mode="ip_ua")
        assert r_ua["pairs"][0]["overlap"] == 0     # different UA -> no match
        r_ip = A.estimate(db, [a, b], dr, mode="ip")
        assert r_ip["pairs"][0]["overlap"] == 1     # plain IP -> counts as overlap
    finally:
        db.close()
