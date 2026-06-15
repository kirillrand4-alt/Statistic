"""Cannibalization detection: GSC page×query competition + SERP confirmation."""
from __future__ import annotations

from datetime import date

from app.providers.base import DateRange, QueryMetricRow
from app.services import cannibalization as C
from app.services.ingest import upsert_query_metrics

D1, D2 = date(2026, 6, 1), date(2026, 6, 2)
DR = DateRange(start=date(2026, 5, 1), end=date(2026, 6, 30))
H = "https://example.com"


def _q(query, url, d, clicks, impr, pos):
    return QueryMetricRow(query=query, url=url, date=d, clicks=clicks, impressions=impr,
                          ctr=(clicks / impr if impr else 0.0), position=pos)


def _seed(db, site):
    rows = [
        # "ремонт": two pages compete; top page flips day1->day2 (instability=2)
        _q("ремонт", f"{H}/a", D1, 6, 60, 4), _q("ремонт", f"{H}/a", D2, 4, 40, 6),
        _q("ремонт", f"{H}/b", D1, 2, 50, 7), _q("ремонт", f"{H}/b", D2, 2, 45, 8),
        # "цена": single page -> not cannibalized
        _q("цена", f"{H}/c", D1, 20, 200, 3),
        # "винт": homepage + one inner page
        _q("винт", f"{H}/", D1, 50, 500, 2), _q("винт", f"{H}/d", D1, 1, 50, 9),
    ]
    upsert_query_metrics(db, site, rows)
    db.commit()


def test_gsc_cannibalization_basic(db, site):
    _seed(db, site)
    rows = C.gsc_cannibalization(db, [site.id], DR, min_query_impr=30, min_page_impr=10)
    by_q = {r["query"]: r for r in rows}
    assert "ремонт" in by_q and "цена" not in by_q
    r = by_q["ремонт"]
    assert r["pages_n"] == 2
    assert r["primary"]["url"].endswith("/a")          # more clicks (10 > 4)
    assert r["cannibals"][0]["url"].endswith("/b")
    assert r["total_impr"] == 195 and r["instability"] == 2


def test_exclude_home_toggle(db, site):
    _seed(db, site)
    assert "винт" not in {r["query"] for r in
                          C.gsc_cannibalization(db, [site.id], DR, min_query_impr=30,
                                                min_page_impr=10, exclude_home=True)}
    assert "винт" in {r["query"] for r in
                      C.gsc_cannibalization(db, [site.id], DR, min_query_impr=30,
                                            min_page_impr=10, exclude_home=False)}


def test_max_position_filter(db, site):
    _seed(db, site)
    # /b avg position ~7.6 -> dropped at max_position=5, leaving one page (not cannibal)
    rows = C.gsc_cannibalization(db, [site.id], DR, min_query_impr=30, min_page_impr=10,
                                 max_position=5)
    assert "ремонт" not in {r["query"] for r in rows}


def test_serp_cannibalization(db):
    from app.db.models import SerpResult
    db.add_all([
        SerpResult(keyword="k1", se=2, region=213, position=2, url="https://shop.ru/a",
                   url_domain="shop.ru", captured_on=D1),
        SerpResult(keyword="k1", se=2, region=213, position=5, url="https://shop.ru/b",
                   url_domain="shop.ru", captured_on=D1),
        SerpResult(keyword="k1", se=2, region=213, position=1, url="https://rival.ru/x",
                   url_domain="rival.ru", captured_on=D1),           # not ours
        SerpResult(keyword="k2", se=2, region=213, position=3, url="https://shop.ru/only",
                   url_domain="shop.ru", captured_on=D1),            # single -> no
        SerpResult(keyword="k3", se=2, region=213, position=1, url="https://shop.ru/",
                   url_domain="shop.ru", captured_on=D1),            # home
        SerpResult(keyword="k3", se=2, region=213, position=4, url="https://shop.ru/c",
                   url_domain="shop.ru", captured_on=D1),
    ])
    db.commit()
    out = C.serp_cannibalization(db, D1.isoformat(), {"shop.ru"}, exclude_home=True)
    kws = {r["keyword"]: r for r in out}
    assert "k1" in kws and "k2" not in kws and "k3" not in kws  # k3: home excluded -> 1 url
    assert kws["k1"]["urls_n"] == 2 and kws["k1"]["best"] == 2
    # keeping home, k3 has 2 urls
    assert "k3" in {r["keyword"] for r in
                    C.serp_cannibalization(db, D1.isoformat(), {"shop.ru"}, exclude_home=False)}


def test_build_export(db, site):
    _seed(db, site)
    rows = C.gsc_cannibalization(db, [site.id], DR, min_query_impr=30, min_page_impr=10)
    fname, buf, _ = C.build_export(rows, "example.com", fmt="csv")
    body = buf.getvalue().decode("utf-8-sig")
    assert fname.endswith(".csv") and "Запрос" in body and "ремонт" in body
    assert "основная" in body and "каннибал" in body
