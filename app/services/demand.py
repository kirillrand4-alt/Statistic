"""Спрос: чтение собранной истории Wordstat (таблица ``wordstat_history``).

Данные собирает ``scripts/wordstat.py`` (помесячная частотность «История запросов»).
Здесь — только чтение для вкладки «Спрос»: список фраз со сводкой и помесячные
ряды для графика, с фильтрами по региону/устройству/периоду и поиском по фразе.
"""
from __future__ import annotations

from datetime import date as date_type

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.db.models import WordstatHistory as W


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


def load(db: Session, region=None, device=None, start=None, end=None, search=None):
    """Return ``(months, phrases)``.

    ``months`` — отсортированные ISO-метки месяцев (ось X графика).
    ``phrases`` — список словарей со сводкой по фразе и рядом ``series`` (значение
    на каждый месяц из ``months``; ``None`` — пропуск). Отсортированы по макс. частоте.
    """
    stmt = _scope(select(W.query, W.date, W.value), region, device, start, end)
    if search:
        stmt = stmt.where(W.query.ilike(f"%{search}%"))
    stmt = stmt.order_by(W.query, W.date)
    by: dict[str, list] = {}
    for q, d, v in db.execute(stmt).all():
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
