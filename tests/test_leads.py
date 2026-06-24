"""Заявки с рекламы (leads): ad-traffic visits that reached a goal."""
from __future__ import annotations

import datetime as dt
import json

from app.bootstrap import ensure_sources
from app.db.base import SessionLocal
from app.db.models import Hit, Site, Visit
from app.providers.base import DateRange
from app.services import leads as L


def _visit(sid, vid, src, goals, wids, su, eu, utm=True):
    extra = {"goalsID": goals}
    if utm:
        extra.update(lastUTMSource="yandex", lastUTMMedium="cpc", lastUTMCampaign="comp")
    return Visit(site_id=sid, visit_id=vid, date=dt.date(2026, 6, 20),
                 date_time="2026-06-20 12:00:00", start_url=su, end_url=eu, watch_ids=wids,
                 extra=json.dumps(extra), traffic_source=src, region_city="Москва", device="desktop")


def _setup():
    db = SessionLocal()
    gsc = ensure_sources(db)["gsc"]
    site = Site(source_id=gsc.id, property_uri="https://prokompressor.ru/", display_name="pk")
    db.add(site)
    db.commit()
    sid = site.id
    db.add_all([
        _visit(sid, "101", "ad", "[111]", "1,2,3",
               "https://prokompressor.ru/lp?utm_source=yandex", "https://prokompressor.ru/thanks"),
        _visit(sid, "102", "organic", "[111]", "4",
               "https://prokompressor.ru/x", "https://prokompressor.ru/x", utm=False),  # not ad
        _visit(sid, "103", "ad", "[222]", "5", "https://prokompressor.ru/lp2",
               "https://prokompressor.ru/done"),  # other goal
    ])
    db.add_all([
        Hit(site_id=sid, watch_id="1", url="https://prokompressor.ru/lp", date=dt.date(2026, 6, 20)),
        Hit(site_id=sid, watch_id="2", url="https://prokompressor.ru/catalog", date=dt.date(2026, 6, 20)),
        Hit(site_id=sid, watch_id="3", url="https://prokompressor.ru/thanks", date=dt.date(2026, 6, 20)),
    ])
    db.commit()
    return db, sid


def test_leads_ad_filter_goal_filter_and_path():
    db, sid = _setup()
    try:
        dr = DateRange(start=dt.date(2026, 6, 1), end=dt.date(2026, 6, 30))
        ids = L.visit_site_ids(db, "prokompressor.ru")
        assert sid in ids

        # only ad visits counted; organic visit's goal is excluded
        avail = {g["id"]: g["visits"] for g in L.available_goals(db, ids, dr)}
        assert avail == {111: 1, 222: 1}

        # goal filter -> only the ad visit that reached goal 111
        rows = L.leads(db, ids, dr, {111})
        assert len(rows) == 1
        r = rows[0]
        assert r["entry"] == "https://prokompressor.ru/lp?utm_source=yandex"
        assert r["goal_page"] == "https://prokompressor.ru/thanks"  # exit = goal page proxy
        assert r["path"] == [
            "https://prokompressor.ru/lp",
            "https://prokompressor.ru/catalog",
            "https://prokompressor.ru/thanks",
        ]
        assert r["utm"]["utm_source"] == "yandex"

        # no goal filter -> all ad visits with a goal (101 + 103), organic excluded
        assert len(L.leads(db, ids, dr, None)) == 2

        # favourites persist per scope
        L.set_favorites(db, "prokompressor.ru", {111})
        assert L.get_favorites(db, "prokompressor.ru") == {111}
    finally:
        db.close()


def test_leads_all_domains_scope():
    db, sid = _setup()
    try:
        dr = DateRange(start=dt.date(2026, 6, 1), end=dt.date(2026, 6, 30))
        assert "prokompressor.ru" in L.domains_with_ads(db, dr)
        # domain=None -> every site holding visits
        assert sid in L.visit_site_ids(db, None)
    finally:
        db.close()
