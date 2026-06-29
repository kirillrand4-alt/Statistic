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

from app.db.models import AppSetting, WordstatHistory as W

KEYLIST_SETTING = "wordstat_keylist"  # newline-joined uploaded phrases (original case)


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
    if hashes:
        db.execute(delete(W).where(W.query_hash.in_(hashes)))
        db.commit()
    kl = get_keylist(db)
    remaining = [k for k in kl if norm_key(k) not in norms]
    if len(remaining) != len(kl):
        affected |= {norm_key(k) for k in kl if norm_key(k) in norms}
        set_keylist(db, "\n".join(remaining))
    return len(affected)


def clear_all(db: Session) -> int:
    """Удалить всю собранную историю Wordstat (и список). Возвращает число строк."""
    n = db.execute(select(func.count()).select_from(W)).scalar() or 0
    db.execute(delete(W))
    db.commit()
    clear_keylist(db)
    return n


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


def regions(db: Session) -> list[str]:
    return [r for (r,) in db.execute(select(distinct(W.region)).order_by(W.region)).all() if r]


def devices(db: Session) -> list[str]:
    return [r for (r,) in db.execute(select(distinct(W.device)).order_by(W.device)).all() if r]


def _scope(stmt, region, device, start=None, end=None):
    if region:
        stmt = stmt.where(W.region == region)
    if device:
        stmt = stmt.where(W.device == device)
    if start:
        stmt = stmt.where(W.date >= start)
    if end:
        stmt = stmt.where(W.date <= end)
    return stmt


def bounds(db: Session, region=None, device=None) -> tuple[date_type | None, date_type | None]:
    """Earliest/latest month present for the given region/device (None if empty)."""
    lo, hi = db.execute(_scope(select(func.min(W.date), func.max(W.date)), region, device)).one()
    return lo, hi


def has_data(db: Session) -> bool:
    return bool(db.execute(select(W.id).limit(1)).first())


def load(db: Session, region=None, device=None, start=None, end=None, search=None, keyset=None):
    """Return ``(months, phrases)``.

    ``months`` — отсортированные ISO-метки месяцев (ось X графика).
    ``phrases`` — список словарей со сводкой по фразе и рядом ``series`` (значение
    на каждый месяц из ``months``; ``None`` — пропуск). Отсортированы по макс. частоте.
    ``keyset`` — если задан (множество нормализованных фраз), оставляем только их.
    """
    stmt = _scope(select(W.query, W.date, W.value), region, device, start, end)
    if search:
        stmt = stmt.where(W.query.ilike(f"%{search}%"))
    stmt = stmt.order_by(W.query, W.date)
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
