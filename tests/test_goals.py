"""Metrica goal completions attributed to the entrance page."""
from __future__ import annotations

import json
from datetime import date

from app.bootstrap import ensure_sources
from app.db.models import Site, Visit
from app.providers.base import DateRange
from app.services.goals import goal_stats, page_key, parse_favorites, parse_goal_ids

DR = DateRange(start=date(2026, 6, 1), end=date(2026, 6, 30))


def test_parse_goal_ids_formats():
    assert parse_goal_ids(json.dumps({"goalsID": "[123,456]"})) == {123, 456}
    assert parse_goal_ids(json.dumps({"goalsID": "789"})) == {789}
    assert parse_goal_ids(json.dumps({"goalsID": "12, 34"})) == {12, 34}
    assert parse_goal_ids(json.dumps({"goalsID": "[]"})) == set()
    assert parse_goal_ids(json.dumps({"device": "desktop"})) == set()
    assert parse_goal_ids(None) == set()
    assert parse_favorites("1, 2 ,3") == {1, 2, 3} and parse_favorites("") == set()


def test_page_key_ignores_scheme_www_query_slash():
    assert page_key("https://www.a.ru/p/?utm=x") == "a.ru/p"
    assert page_key("http://a.ru/p") == "a.ru/p"
    assert page_key("https://a.ru/") == "a.ru/"


def _visit(db, sid, vid, start_url, goal_ids, d=date(2026, 6, 10)):
    extra = json.dumps({"goalsID": "[" + ",".join(map(str, goal_ids)) + "]"}) if goal_ids else None
    db.add(Visit(site_id=sid, visit_id=str(vid), date=d, start_url=start_url,
                 counter_id=999, extra=extra))


def test_goal_stats_by_entrance_page_and_favorites(db):
    src = ensure_sources(db)["yandex_webmaster"]
    s = Site(source_id=src.id, property_uri="https://shop.ru/", display_name="shop.ru")
    db.add(s)
    db.commit()
    # project tracks /a and /b ; landings with UTM/www must still match
    url_for_key = {page_key("https://shop.ru/a"): "https://shop.ru/a",
                   page_key("https://shop.ru/b"): "https://shop.ru/b"}

    _visit(db, s.id, 1, "https://www.shop.ru/a?utm=ya", [10, 20])  # entered /a, goals 10 & 20
    _visit(db, s.id, 2, "https://shop.ru/a/", [10])               # entered /a, goal 10
    _visit(db, s.id, 3, "https://shop.ru/b", [20])                # entered /b, goal 20
    _visit(db, s.id, 4, "https://shop.ru/other", [10])            # not a project page
    _visit(db, s.id, 5, "https://shop.ru/a", [])                  # no goal
    db.commit()

    # all goals (no favourites): 10 -> 2 (visits 1,2), 20 -> 2 (visits 1,3) = 4 total
    allg = goal_stats(db, [s.id], DR, url_for_key, favorites=None)
    by_goal = {g["id"]: g["count"] for g in allg["by_goal"]}
    assert by_goal == {10: 2, 20: 2} and allg["total"] == 4
    by_url = {r["url"]: r["count"] for r in allg["by_url"]}
    assert by_url == {"https://shop.ru/a": 3, "https://shop.ru/b": 1}  # /a: 2+1, /b: 1
    assert {g["id"] for g in allg["available"]} == {10, 20}

    # favourite = goal 20 only -> 2 completions (visits 1 and 3)
    fav = goal_stats(db, [s.id], DR, url_for_key, favorites={20})
    assert fav["total"] == 2
    assert {g["id"]: g["count"] for g in fav["by_goal"]} == {20: 2}
    assert {r["url"]: r["count"] for r in fav["by_url"]} == {"https://shop.ru/a": 1, "https://shop.ru/b": 1}


def test_goal_stats_dedups_visit_across_properties(db):
    """A domain's visits can sit under several same-domain properties
    (https:// + sc-domain:). Each visit must be counted ONCE, not per property."""
    src = ensure_sources(db)["yandex_webmaster"]
    a = Site(source_id=src.id, property_uri="https://shop.ru/", display_name="a")
    b = Site(source_id=src.id, property_uri="sc-domain:shop.ru", display_name="b")
    db.add_all([a, b])
    db.commit()
    ufk = {page_key("https://shop.ru/p"): "https://shop.ru/p"}
    _visit(db, a.id, 1, "https://shop.ru/p", [10])   # same visit id 1 under BOTH
    _visit(db, b.id, 1, "https://shop.ru/p", [10])
    db.commit()

    s = goal_stats(db, [a.id, b.id], DR, ufk)
    assert s["total"] == 1
    assert {g["id"]: g["count"] for g in s["by_goal"]} == {10: 1}
