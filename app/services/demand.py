"""Спрос: чтение собранной истории Wordstat (таблица ``wordstat_history``).

Данные собирает ``scripts/wordstat.py`` (помесячная частотность «История запросов»).
Здесь — только чтение для вкладки «Спрос»: список фраз со сводкой и помесячные
ряды для графика, с фильтрами по региону/устройству/периоду и поиском по фразе.
"""
from __future__ import annotations

import re
from datetime import date as date_type

from sqlalchemy import delete, distinct, func, select
from sqlalchemy.orm import Session

from app.db.models import AppSetting, WordstatHistory as W, WordstatSeries as WS

KEYLIST_SETTING = "wordstat_keylist"  # newline-joined uploaded phrases (original case)


def _src(granularity="month"):
    """Return (Model, base_conditions) for a granularity: month → wordstat_history,
    day/week → wordstat_series filtered by granularity."""
    if granularity in ("day", "week"):
        return WS, [WS.granularity == granularity]
    return W, []


def norm_key(s: str) -> str:
    """Normalize a phrase for matching: lowercase, ё→е, collapse whitespace."""
    return re.sub(r"\s+", " ", (s or "").strip().lower().replace("ё", "е"))


def parse_keylist(raw: str) -> list[str]:
    """Split pasted/uploaded text into phrases (one per line), de-duplicated."""
    out, seen = [], set()
    for line in (raw or "").replace("\r", "\n").split("\n"):
        line = line.strip()
        if line and norm_key(line) not in seen:
            seen.add(norm_key(line))
            out.append(line)
    return out


def get_keylist(db: Session) -> list[str]:
    row = db.get(AppSetting, KEYLIST_SETTING)
    return parse_keylist(row.value) if row and row.value else []


def set_keylist(db: Session, raw: str) -> list[str]:
    phrases = parse_keylist(raw)
    row = db.get(AppSetting, KEYLIST_SETTING)
    val = "\n".join(phrases)
    if row is None:
        db.add(AppSetting(key=KEYLIST_SETTING, value=val))
    else:
        row.value = val
    db.commit()
    return phrases


def clear_keylist(db: Session) -> None:
    row = db.get(AppSetting, KEYLIST_SETTING)
    if row is not None:
        db.delete(row)
        db.commit()


def delete_phrases(db: Session, phrases) -> int:
    """Полностью удалить фразы: их собранную историю Wordstat (по всем регионам/
    устройствам) и запись в загруженном списке. Сопоставление по нормализованной
    фразе (регистр/ё/пробелы), а не по точному совпадению. Возвращает число фраз."""
    phrases = [p for p in (phrases or []) if p and p.strip()]
    if not phrases:
        return 0
    norms = {norm_key(p) for p in phrases}
    pairs = db.execute(select(W.query, W.query_hash).distinct()).all()
    hashes = {h for (qq, h) in pairs if norm_key(qq) in norms}
    affected = {norm_key(qq) for (qq, _h) in pairs if norm_key(qq) in norms}
    # also match fine-grained (day/week) rows by normalized query
    for (qq, h) in db.execute(select(WS.query, WS.query_hash).distinct()).all():
        if norm_key(qq) in norms:
            hashes.add(h)
            affected.add(norm_key(qq))
    if hashes:
        db.execute(delete(W).where(W.query_hash.in_(hashes)))
        db.execute(delete(WS).where(WS.query_hash.in_(hashes)))
        db.commit()
    kl = get_keylist(db)
    remaining = [k for k in kl if norm_key(k) not in norms]
    if len(remaining) != len(kl):
        affected |= {norm_key(k) for k in kl if norm_key(k) in norms}
        set_keylist(db, "\n".join(remaining))
    return len(affected)


def clear_all(db: Session) -> int:
    """Удалить всю собранную историю Wordstat — месячную и дневную/недельную (и
    список ключей). Возвращает число удалённых строк."""
    n = (db.execute(select(func.count()).select_from(W)).scalar() or 0)
    n += (db.execute(select(func.count()).select_from(WS)).scalar() or 0)
    db.execute(delete(W))
    db.execute(delete(WS))
    db.commit()
    clear_keylist(db)
    return n


def dedup_groups(phrases):
    """Свернуть смысловые дубли — фразы с ПОЛНОСТЬЮ совпадающим помесячным рядом
    (Wordstat отдаёт одинаковую историю для перестановок слов: «винтовой компрессор»
    = «компрессор винтовой»). Возвращает по одному представителю на группу (с самой
    высокой частотой, затем покороче), у каждого — список ``dupes`` свёрнутых фраз.
    Все фразы из одного :func:`load` имеют общую ось месяцев, поэтому ряды сравнимы.
    """
    groups: dict[tuple, list] = {}
    for p in phrases:
        key = tuple(p["series"])  # exact monthly series → semantic duplicate
        groups.setdefault(key, []).append(p)
    reps = []
    for members in groups.values():
        members.sort(key=lambda x: (-x["max"], len(x["query"]), x["query"]))
        rep = dict(members[0])
        rep["dupes"] = [m["query"] for m in members[1:]]
        reps.append(rep)
    reps.sort(key=lambda p: p["max"], reverse=True)
    return reps


def sum_series(phrases) -> list[int]:
    """Element-wise sum of the per-month ``series`` across phrases (None → 0).

    All phrases from one :func:`load` call share the same month axis, so the
    result aligns to that call's ``months``. Used for the «по всем фразам» line.
    """
    if not phrases:
        return []
    n = len(phrases[0]["series"])
    out = [0] * n
    for p in phrases:
        for i, v in enumerate(p["series"]):
            if v is not None:
                out[i] += v
    return out


def aggregate(label: str, phrases) -> dict | None:
    """One summary-shaped dataset = sum of ``phrases`` (for the chart). None if empty."""
    if not phrases:
        return None
    series = sum_series(phrases)
    present = [v for v in series if v]
    return {
        "query": label,
        "series": series,
        "count": len(phrases),
        "max": max(series) if series else 0,
        "last": next((v for v in reversed(series) if v is not None), 0),
        "avg": round(sum(present) / len(present)) if present else 0,
    }


def regions(db: Session, granularity="month") -> list[str]:
    M, base = _src(granularity)
    stmt = select(distinct(M.region)).order_by(M.region)
    for c in base:
        stmt = stmt.where(c)
    return [r for (r,) in db.execute(stmt).all() if r]


def devices(db: Session, granularity="month") -> list[str]:
    M, base = _src(granularity)
    stmt = select(distinct(M.device)).order_by(M.device)
    for c in base:
        stmt = stmt.where(c)
    return [r for (r,) in db.execute(stmt).all() if r]


def _scope(stmt, M, base, region, device, start=None, end=None):
    for c in base:
        stmt = stmt.where(c)
    if region:
        stmt = stmt.where(M.region == region)
    if device:
        stmt = stmt.where(M.device == device)
    if start:
        stmt = stmt.where(M.date >= start)
    if end:
        stmt = stmt.where(M.date <= end)
    return stmt


def bounds(db: Session, region=None, device=None,
           granularity="month") -> tuple[date_type | None, date_type | None]:
    """Earliest/latest point present for the given region/device/granularity."""
    M, base = _src(granularity)
    lo, hi = db.execute(_scope(select(func.min(M.date), func.max(M.date)), M, base,
                               region, device)).one()
    return lo, hi


def has_data(db: Session, granularity="month") -> bool:
    M, base = _src(granularity)
    stmt = select(M.id).limit(1)
    for c in base:
        stmt = stmt.where(c)
    return bool(db.execute(stmt).first())


def load(db: Session, region=None, device=None, start=None, end=None, search=None,
         keyset=None, granularity="month"):
    """Return ``(points, phrases)``.

    ``points`` — отсортированные ISO-метки периода (ось X: месяцы/недели/дни).
    ``phrases`` — список словарей со сводкой по фразе и рядом ``series`` (значение
    на каждую точку из ``points``; ``None`` — пропуск). Отсортированы по макс. частоте.
    ``keyset`` — если задан (множество нормализованных фраз), оставляем только их.
    ``granularity`` — month | week | day (источник — соотв. таблица).
    """
    M, base = _src(granularity)
    stmt = _scope(select(M.query, M.date, M.value), M, base, region, device, start, end)
    if search:
        stmt = stmt.where(M.query.ilike(f"%{search}%"))
    stmt = stmt.order_by(M.query, M.date)
    by: dict[str, list] = {}
    for q, d, v in db.execute(stmt).all():
        if keyset is not None and norm_key(q) not in keyset:
            continue
        by.setdefault(q, []).append((d, int(v or 0)))

    months = sorted({d for pts in by.values() for d, _ in pts})
    keys = [d.isoformat() for d in months]

    phrases = []
    for q, pts in by.items():
        pts.sort()
        vals = [v for _, v in pts]
        per_month = {d.isoformat(): v for d, v in pts}
        first, last = vals[0], vals[-1]
        change = round((last - first) / first * 100) if first else None
        phrases.append({
            "query": q,
            "points": len(vals),
            "min": min(vals),
            "avg": round(sum(vals) / len(vals)),
            "max": max(vals),
            "first": first,
            "last": last,
            "change": change,
            "series": [per_month.get(k) for k in keys],
        })
    phrases.sort(key=lambda p: p["max"], reverse=True)
    return keys, phrases
