"""Yandex Metrica visits: import (Logs API TSV / uploaded file) + analysis."""
from __future__ import annotations

import csv
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Visit
from app.providers.base import DateRange

# Metrica Logs API field -> our column
FIELD_MAP = {
    "ym:s:visitID": "visit_id",
    "ym:s:counterID": "counter_id",
    "ym:s:date": "date",
    "ym:s:dateTime": "date_time",
    "ym:s:clientID": "client_id",
    "ym:s:lastTrafficSource": "traffic_source",
    "ym:s:lastSearchEngine": "search_engine",
    "ym:s:lastAdvEngine": "adv_engine",
    "ym:s:referer": "referer",
    "ym:s:startURL": "start_url",
    "ym:s:endURL": "end_url",
    "ym:s:pageViews": "page_views",
    "ym:s:visitDuration": "duration",
    "ym:s:bounce": "bounce",
    "ym:s:deviceCategory": "device",
    "ym:s:operatingSystem": "os",
    "ym:s:browser": "browser",
    "ym:s:regionCity": "region_city",
    "ym:s:ipAddress": "ip",
    "ym:s:watchIDs": "watch_ids",
}
_SHORT = {k.split(":")[-1]: v for k, v in FIELD_MAP.items()}
_INT_COLS = {"visit_id", "counter_id", "page_views", "duration", "bounce"}


def _toint(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _todate(v):
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _insert_ignore(db: Session, rows: list[dict]) -> int:
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:  # pragma: no cover
        raise RuntimeError(f"Unsupported dialect {dialect!r}")
    stmt = insert(Visit).values(rows).on_conflict_do_nothing(index_elements=["site_id", "visit_id"])
    db.execute(stmt)
    return len(rows)


def import_tsv(db: Session, site_id: int, lines) -> int:
    """Parse a Metrica visits TSV (header of ym:s:* fields) into the Visit table."""
    reader = csv.reader(lines, delimiter="\t")
    try:
        header = next(reader)
    except StopIteration:
        return 0
    cols = []
    for i, name in enumerate(header):
        col = FIELD_MAP.get(name) or _SHORT.get(name.split(":")[-1])
        if col:
            cols.append((i, col))
    if not any(c == "visit_id" for _, c in cols):
        raise ValueError("В файле не найден столбец ym:s:visitID — это не выгрузка визитов Метрики.")

    written, payload = 0, []
    for row in reader:
        rec = {"site_id": site_id}
        for i, col in cols:
            if i < len(row):
                rec[col] = row[i]
        if not rec.get("visit_id"):
            continue
        for c in _INT_COLS:
            rec[c] = _toint(rec.get(c))
        rec["date"] = _todate(rec.get("date"))
        for c in ("date_time", "client_id", "traffic_source", "search_engine", "adv_engine",
                  "referer", "start_url", "end_url", "device", "os", "browser",
                  "region_city", "ip", "watch_ids"):
            if c in rec and rec[c] == "":
                rec[c] = None
        payload.append(rec)
        if len(payload) >= 1000:
            written += _insert_ignore(db, payload)
            payload = []
    if payload:
        written += _insert_ignore(db, payload)
    db.commit()
    return written


def summary(db: Session, site_id: int, dr: DateRange) -> dict:
    where = (Visit.site_id == site_id, Visit.date >= dr.start, Visit.date <= dr.end)
    total, pv, dur, bnc = db.execute(
        select(func.count(), func.coalesce(func.sum(Visit.page_views), 0),
               func.coalesce(func.avg(Visit.duration), 0.0),
               func.coalesce(func.avg(Visit.bounce), 0.0)).where(*where)
    ).one()

    def breakdown(col, limit=15):
        rows = db.execute(
            select(col, func.count()).where(*where).group_by(col)
            .order_by(func.count().desc()).limit(limit)
        ).all()
        return [{"key": (k or "—"), "count": c} for k, c in rows]

    return {
        "total": total or 0,
        "page_views": int(pv or 0),
        "avg_duration": round(float(dur or 0.0), 1),
        "bounce_rate": round(float(bnc or 0.0) * 100, 1),
        "by_source": breakdown(Visit.traffic_source),
        "by_device": breakdown(Visit.device),
        "by_search": breakdown(Visit.search_engine),
    }
