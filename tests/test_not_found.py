"""404 analytics from Metrica hits: title detection, channel split, referrers."""
from __future__ import annotations

from datetime import date

from app.bootstrap import ensure_sources
from app.db.models import Hit, Site
from app.providers.base import DateRange
from app.services.not_found import (
    not_found_overview,
    not_found_stats,
    parse_markers,
)

DR = DateRange(start=date(2026, 6, 1), end=date(2026, 6, 30))


def _site(db, host: str) -> int:
    src = ensure_sources(db)["yandex_webmaster"]
    s = Site(source_id=src.id, property_uri=f"https://{host}/", display_name=host)
    db.add(s)
    db.commit()
    return s.id


def _hit(db, sid, wid, title, url, referer, ts, *, pv=1, d=date(2026, 6, 10)):
    db.add(Hit(site_id=sid, watch_id=wid, date=d, title=title, url=url,
               referer=referer, traffic_source=ts, is_page_view=pv))


def test_parse_markers_default_and_custom():
    assert "404" in parse_markers(None)
    assert parse_markers(" foo , bar ,, ") == ["foo", "bar"]
    assert parse_markers("") == parse_markers(None)  # blank -> defaults


def test_not_found_detection_channels_and_referrers(db):
    sid = _site(db, "shop.ru")
    # four real 404 pageviews (different channels), plus noise that must be ignored
    _hit(db, sid, 1, "Страница не найдена", "/old", "https://ads.x/y", "ad")          # ad
    _hit(db, sid, 2, "Ошибка 404", "/old", "https://yandex.ru/search", "organic")     # search
    _hit(db, sid, 3, "404 — не найдена", "/gone", "https://shop.ru/catalog", "internal")  # internal
    _hit(db, sid, 4, "Страница не найдена", "/gone", "", "direct")                     # direct
    _hit(db, sid, 5, "Главная", "/", "", "organic")                                   # not a 404
    _hit(db, sid, 6, "Страница не найдена", "/dl", "", "ad", pv=0)                     # not a pageview
    db.commit()

    s = not_found_stats(db, [sid], DR, site_domain="shop.ru")

    assert s["total"] == 4 and s["unique_urls"] == 2
    assert s["ad"] == 1 and s["search"] == 1 and s["direct"] == 1
    chan = {r["key"]: r["count"] for r in s["by_channel"]}
    assert chan == {"Реклама": 1, "Поиск": 1, "Внутренние": 1, "Прямые": 1}

    # referrer totals: one internal (shop.ru), one direct (empty), two external
    assert s["from_internal"] == 1 and s["from_direct"] == 1 and s["from_external"] == 2
    kinds = {r["referer"]: r["kind"] for r in s["top_referers"]}
    assert kinds["https://shop.ru/catalog"] == "internal"
    assert kinds["https://yandex.ru/search"] == "external"
    assert kinds["(прямой / без реферера)"] == "none"

    # per-URL channel split
    by_url = {u["url"]: u for u in s["top_urls"]}
    assert by_url["/old"]["ad"] == 1 and by_url["/old"]["search"] == 1
    assert by_url["/gone"]["direct"] == 1 and by_url["/gone"]["other"] == 1  # internal -> other


def test_not_found_overview_last_day_vs_previous(db):
    dr = DateRange(start=date(2026, 6, 1), end=date(2026, 6, 11))
    d1, d2 = date(2026, 6, 4), date(2026, 6, 5)  # last two days with data (sync lags)
    a, b = _site(db, "a.ru"), _site(db, "b.ru")
    # a.ru: 2 on its prev day (06-04), 3 on its last day (06-05) -> +50%
    _hit(db, a, 1, "404", "/x", "", "organic", d=d1)
    _hit(db, a, 2, "404", "/x", "", "ad", d=d1)
    _hit(db, a, 3, "404", "/y", "", "organic", d=d2)
    _hit(db, a, 4, "404", "/y", "", "organic", d=d2)
    _hit(db, a, 5, "404", "/z", "", "ad", d=d2)
    # b.ru: a single 404 day -> no previous day
    _hit(db, b, 6, "Страница не найдена", "/p", "", "direct", d=d1)
    db.commit()

    ov = not_found_overview(db, dr)
    by_dom = {r["domain"]: r for r in ov}
    assert set(by_dom) == {"a.ru", "b.ru"}
    assert ov[0]["domain"] == "a.ru"  # sorted by last-day count desc
    assert by_dom["a.ru"]["last"] == 3 and by_dom["a.ru"]["prev"] == 2
    assert by_dom["a.ru"]["last_date"] == "2026-06-05" and by_dom["a.ru"]["prev_date"] == "2026-06-04"
    assert by_dom["a.ru"]["delta_pct"] == 50.0 and by_dom["a.ru"]["total"] == 5
    assert by_dom["b.ru"]["last"] == 1 and by_dom["b.ru"]["prev_date"] is None
    assert by_dom["b.ru"]["delta_pct"] is None  # single day -> "новые"
    assert len(by_dom["a.ru"]["daily"]) == (dr.end - dr.start).days + 1


def test_not_found_empty_and_custom_markers(db):
    sid = _site(db, "blog.ru")
    _hit(db, sid, 10, "Тут пусто, sorry", "/x", "", "organic")
    db.commit()

    assert not_found_stats(db, [sid], DR, site_domain="blog.ru")["total"] == 0  # no marker hit
    # a custom marker matches it (case-insensitive)
    s = not_found_stats(db, [sid], DR, markers_raw="SORRY", site_domain="blog.ru")
    assert s["total"] == 1
