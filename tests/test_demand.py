"""Спрос: чтение собранной истории Wordstat (wordstat_history)."""
from __future__ import annotations

import datetime as dt

from app.db.base import SessionLocal
from app.db.models import WordstatHistory
from app.services import demand
from app.utils import query_hash


def _row(q, region, device, y, m, v):
    return WordstatHistory(query=q, query_hash=query_hash(q), region=region,
                           device=device, date=dt.date(y, m, 1), value=v)


def _setup():
    db = SessionLocal()
    db.add_all([
        _row("компрессор купить", "all", "all", 2025, 1, 100),
        _row("компрессор купить", "all", "all", 2025, 2, 150),
        _row("компрессор купить", "all", "all", 2025, 3, 200),
        _row("ресивер 500л", "all", "all", 2025, 1, 40),
        _row("ресивер 500л", "all", "all", 2025, 3, 30),  # gap in 2025-02
        _row("компрессор купить", "msk", "all", 2025, 1, 9),  # other region
    ])
    db.commit()
    return db


def test_regions_devices_and_bounds():
    db = _setup()
    try:
        assert demand.regions(db) == ["all", "msk"]
        assert demand.devices(db) == ["all"]
        lo, hi = demand.bounds(db, "all", "all")
        assert (lo, hi) == (dt.date(2025, 1, 1), dt.date(2025, 3, 1))
    finally:
        db.close()


def test_load_stats_and_series_alignment():
    db = _setup()
    try:
        months, phrases = demand.load(db, "all", "all", None, None)
        assert months == ["2025-01-01", "2025-02-01", "2025-03-01"]
        # sorted by max desc -> компрессор first
        by_q = {p["query"]: p for p in phrases}
        k = by_q["компрессор купить"]
        assert k["points"] == 3
        assert k["min"] == 100 and k["max"] == 200 and k["avg"] == 150
        assert k["first"] == 100 and k["last"] == 200
        assert k["change"] == 100  # 100 -> 200 = +100%
        assert k["series"] == [100, 150, 200]
        # ресивер has a gap in Feb -> None aligned to months
        r = by_q["ресивер 500л"]
        assert r["series"] == [40, None, 30]
        assert r["change"] == -25  # 40 -> 30
    finally:
        db.close()


def test_region_and_search_filters():
    db = _setup()
    try:
        # msk region only has the one phrase point
        _, phrases = demand.load(db, "msk", "all", None, None)
        assert len(phrases) == 1 and phrases[0]["max"] == 9
        # period filter excludes the first month
        months, _ = demand.load(db, "all", "all", dt.date(2025, 2, 1), None)
        assert months == ["2025-02-01", "2025-03-01"]
        # substring search
        _, only = demand.load(db, "all", "all", None, None, "ресивер")
        assert [p["query"] for p in only] == ["ресивер 500л"]
    finally:
        db.close()


def test_sum_series_and_aggregate():
    db = _setup()
    try:
        months, phrases = demand.load(db, "all", "all", None, None)
        # элементная сумма по месяцам; None (пропуск у ресивера в феврале) = 0
        # компрессор [100,150,200] + ресивер [40,None,30] -> [140,150,230]
        assert demand.sum_series(phrases) == [140, 150, 230]
        agg = demand.aggregate("Всего", phrases)
        assert agg["query"] == "Всего" and agg["count"] == 2
        assert agg["series"] == [140, 150, 230]
        assert agg["max"] == 230 and agg["last"] == 230
        assert demand.aggregate("x", []) is None
        assert demand.sum_series([]) == []
    finally:
        db.close()


def test_dedup_groups_collapses_identical_series():
    db = SessionLocal()
    try:
        # two phrases with the SAME monthly series (word-order duplicates) + a distinct one
        for q in ("винтовой компрессор", "компрессор винтовой"):
            for m, v in ((1, 37048), (2, 53426), (3, 65870)):
                db.add(_row(q, "all", "all", 2025, m, v))
        for m, v in ((1, 35283), (2, 51756), (3, 60391)):
            db.add(_row("поршневой компрессор", "all", "all", 2025, m, v))
        db.commit()
        _, phrases = demand.load(db, "all", "all", None, None)
        assert len(phrases) == 3
        reps = demand.dedup_groups(phrases)
        assert len(reps) == 2  # the identical pair collapses to one
        wintovoy = next(r for r in reps if r["max"] == 65870)
        # representative is the shorter of the two equal-max phrases; the other is a dupe
        assert wintovoy["query"] in ("винтовой компрессор", "компрессор винтовой")
        assert len(wintovoy["dupes"]) == 1
        assert set([wintovoy["query"], *wintovoy["dupes"]]) == {"винтовой компрессор", "компрессор винтовой"}
        # the distinct phrase has no dupes
        porsh = next(r for r in reps if r["max"] == 60391)
        assert porsh["dupes"] == []
        # summing deduped reps counts the pair once (not twice)
        assert demand.sum_series(reps) == [37048 + 35283, 53426 + 51756, 65870 + 60391]
    finally:
        db.close()


def test_keylist_storage_and_filter():
    db = _setup()
    try:
        # parsing: one per line, dedup (case/ё/whitespace-insensitive), keep order
        phrases = demand.set_keylist(
            db, "Компрессор  Купить\nресивер 500л\nкомпрессор купить\n\n  ")
        assert phrases == ["Компрессор  Купить", "ресивер 500л"]
        assert demand.get_keylist(db) == phrases

        # filter to the saved list (normalized match, incl. odd spacing/case)
        keyset = {demand.norm_key(k) for k in demand.get_keylist(db)}
        _, only = demand.load(db, "all", "all", None, None, keyset=keyset)
        names = {p["query"] for p in only}
        assert names == {"компрессор купить", "ресивер 500л"}

        # a list with an uncollected key -> it just doesn't appear
        demand.set_keylist(db, "компрессор купить\nнесобранный ключ")
        keyset = {demand.norm_key(k) for k in demand.get_keylist(db)}
        _, only = demand.load(db, "all", "all", None, None, keyset=keyset)
        assert {p["query"] for p in only} == {"компрессор купить"}

        demand.clear_keylist(db)
        assert demand.get_keylist(db) == []
    finally:
        db.close()


def test_delete_phrases_removes_data_and_list_entry():
    db = _setup()
    try:
        demand.set_keylist(db, "компрессор купить\nресивер 500л")
        # delete one phrase -> its data gone (all regions) and removed from the list
        n = demand.delete_phrases(db, ["Компрессор Купить"])  # case/space-insensitive
        assert n == 1
        _, phrases = demand.load(db, "all", "all", None, None)
        assert "компрессор купить" not in {p["query"] for p in phrases}
        assert "ресивер 500л" in {p["query"] for p in phrases}
        # also gone from the msk region (delete spans all regions/devices)
        _, msk = demand.load(db, "msk", "all", None, None)
        assert msk == []
        assert demand.get_keylist(db) == ["ресивер 500л"]
        # empty input is a no-op
        assert demand.delete_phrases(db, []) == 0
    finally:
        db.close()


def test_delete_data_keeps_keylist():
    db = _setup()
    try:
        demand.set_keylist(db, "компрессор купить\nресивер 500л")
        # delete data for one phrase, keeping the keylist intact
        n = demand.delete_data(db, ["Компрессор Купить"])  # case/space-insensitive
        assert n == 1
        _, phrases = demand.load(db, "all", "all", None, None)
        assert "компрессор купить" not in {p["query"] for p in phrases}   # data gone
        assert demand.get_keylist(db) == ["компрессор купить", "ресивер 500л"]  # list kept
        assert demand.delete_data(db, []) == 0                             # no-op
    finally:
        db.close()


def test_clear_all():
    db = _setup()
    try:
        demand.set_keylist(db, "компрессор купить")
        n = demand.clear_all(db)
        assert n >= 1
        assert not demand.has_data(db)
        assert demand.get_keylist(db) == []
    finally:
        db.close()
