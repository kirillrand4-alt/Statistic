"""Per-page desktop/mobile metrics with period-over-period change."""
from __future__ import annotations

from datetime import date

from app.providers.base import DateRange, DeviceMetricRow
from app.services import page_devices as PD
from app.services.ingest import upsert_device_metrics

A = DateRange(start=date(2026, 6, 10), end=date(2026, 6, 16))
B = DateRange(start=date(2026, 6, 3), end=date(2026, 6, 9))
H = "https://example.com"


def _d(url, dev, day, clicks, impr):
    return DeviceMetricRow(url=url, date=day, device=dev, clicks=clicks, impressions=impr,
                           ctr=(clicks / impr if impr else 0.0), position=10.0)


def _seed(db, site):
    upsert_device_metrics(db, site, [
        # /a: desktop up (40->50), mobile down (35->30); total up (75->80)
        _d(f"{H}/a", "desktop", date(2026, 6, 12), 50, 1000),
        _d(f"{H}/a", "desktop", date(2026, 6, 5), 40, 1000),
        _d(f"{H}/a", "mobile", date(2026, 6, 12), 30, 900),
        _d(f"{H}/a", "mobile", date(2026, 6, 5), 35, 900),
        # /b: declined (5 vs 20), low impressions
        _d(f"{H}/b", "desktop", date(2026, 6, 12), 5, 100),
        _d(f"{H}/b", "desktop", date(2026, 6, 5), 20, 100),
    ])
    db.commit()


def test_device_compare_deltas_and_improved(db, site):
    _seed(db, site)
    res = PD.page_device_compare(db, [site.id], A, B)
    rows = {r["url"]: r for r in res["rows"]}
    a = rows[f"{H}/a"]
    assert a["improved"] is True
    assert a["total"]["clicks_a"] == 80 and a["total"]["clicks_b"] == 75
    assert a["total"]["clicks_delta"] == 5
    assert a["dev"]["desktop"]["clicks_delta"] == 10      # 50 - 40
    assert a["dev"]["mobile"]["clicks_delta"] == -5       # 30 - 35
    assert abs(a["dev"]["desktop"]["ctr_a"] - 0.05) < 1e-9
    b = rows[f"{H}/b"]
    assert b["improved"] is False and b["total"]["clicks_delta"] == -15
    # default sort = current clicks desc -> /a first
    assert res["rows"][0]["url"] == f"{H}/a"


def test_min_impressions_and_sort(db, site):
    _seed(db, site)
    # /b max impressions 100 -> dropped at min 200
    res = PD.page_device_compare(db, [site.id], A, B, min_impressions=200)
    assert {r["url"] for r in res["rows"]} == {f"{H}/a"}
    # 'drop' sort -> biggest losers first
    res2 = PD.page_device_compare(db, [site.id], A, B, sort="drop")
    assert res2["rows"][0]["url"] == f"{H}/b"
    # search filter
    res3 = PD.page_device_compare(db, [site.id], A, B, search="/b")
    assert {r["url"] for r in res3["rows"]} == {f"{H}/b"}


def test_page_ids_filter(db, site):
    """The 'my pages' upload resolves to page_ids and restricts the report."""
    _seed(db, site)
    from sqlalchemy import select

    from app.db.models import Page
    from app.utils import normalize_url
    pid = db.execute(
        select(Page.id).where(Page.site_id == site.id,
                              Page.normalized_url == normalize_url(f"{H}/a"))
    ).scalar_one()
    res = PD.page_device_compare(db, [site.id], A, B, page_ids=[pid])
    assert {r["url"] for r in res["rows"]} == {f"{H}/a"}
    # empty list (list set but nothing matched) -> no rows
    assert PD.page_device_compare(db, [site.id], A, B, page_ids=[])["rows"] == []


def test_export(db, site):
    _seed(db, site)
    res = PD.page_device_compare(db, [site.id], A, B)
    fname, buf, _ = PD.build_export(res["rows"], res["devices"], "example.com", fmt="csv")
    body = buf.getvalue().decode("utf-8-sig")
    assert fname.endswith(".csv") and "URL" in body and "Десктоп клики A" in body
    assert "example.com/a" in body
