"""Pull, store, query and export the Miralinks donor catalog (``donor_site``)."""
from __future__ import annotations

import io
import threading
import time
from datetime import date

import pandas as pd
from sqlalchemy import func, select

from app.db.base import SessionLocal, init_db
from app.db.models import DonorSite
from app.providers.miralinks import DEFAULT_URL, Miralinks, parse_rows, total_records

CSV_MEDIA = "text/csv"
XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Columns that come straight from a mapped row (everything except identity keys).
_UPSERT_COLS = (
    "domain", "site_url", "name", "description", "sqi", "cy", "da", "ahrefs_dr",
    "cf", "tf", "spamness", "indexed_percent", "ya_indexed_count",
    "google_indexed_count", "traffic", "traffic_interval", "ahrefs_traffic",
    "ahrefs_domains", "ahrefs_keywords", "price_rur", "price_usd",
    "article_price_rur", "region_id", "region", "topics", "lang",
    "links_in_articles", "articles_count", "rating", "placement_time_min",
    "last_placement", "venality", "is_exclusive", "is_fast", "is_pr", "trusted",
    "screenshot", "raw", "captured_on", "fetched_at",
)

# Live progress of the (single) web-launched catalog pull.
_STATUS: dict = {"running": False, "total": 0, "fetched": 0, "stored": 0,
                 "started": 0.0, "msg": ""}
_LOCK = threading.Lock()


def current_status() -> dict:
    return dict(_STATUS)


def store_rows(db, rows, source: str = "miralinks") -> int:
    """Upsert mapped donor rows keyed by (source, external_id)."""
    from datetime import datetime, timezone

    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    today = date.today()
    now = datetime.now(timezone.utc)
    payload = []
    for r in rows:
        if not r.get("external_id") or not r.get("domain"):
            continue
        row = {"source": source, "external_id": r["external_id"], "captured_on": today,
               "fetched_at": now}
        for c in _UPSERT_COLS:
            if c in r:
                row[c] = r[c]
        payload.append(row)
    if not payload:
        return 0
    ins = sqlite_insert
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        ins = pg_insert
    for i in range(0, len(payload), 500):
        chunk = payload[i:i + 500]
        stmt = ins(DonorSite).values(chunk)
        upd = {c: getattr(stmt.excluded, c) for c in chunk[0]
               if c not in ("source", "external_id")}
        stmt = stmt.on_conflict_do_update(
            index_elements=["source", "external_id"], set_=upd)
        db.execute(stmt)
    db.commit()
    return len(payload)


def run_catalog(cookie, body, *, url=None, length=100, pause=1.5, max_records=0,
                max_pages=3000, log=lambda *_: None) -> dict:
    """Page through the whole filtered catalog and upsert it. Returns a summary.

    Stops on an empty page, when ``max_records`` is reached, or on an auth error
    (expired cookie) — which is surfaced rather than silently yielding nothing.
    """
    init_db()
    client = Miralinks(cookie, body, url=url or DEFAULT_URL)
    db = SessionLocal()
    fetched = stored = start = pages = 0
    total = 0
    error = None
    try:
        while pages < max_pages:
            payload = client.fetch_page(start, length)
            if payload.get("error"):
                error = payload.get("detail") or payload.get("error")
                log(f"Ошибка: {error}")
                break
            if not total:
                total = total_records(payload)
                _STATUS["total"] = total
                log(f"Всего под фильтром: {total} площадок")
            rows = list(parse_rows(payload))
            if not rows:
                break
            n = store_rows(db, rows)
            fetched += len(rows)
            stored += n
            start += len(rows)
            pages += 1
            _STATUS.update(fetched=fetched, stored=stored)
            log(f"  стр.{pages}: +{len(rows)} (всего {fetched}"
                + (f"/{total}" if total else "") + ")")
            if max_records and fetched >= max_records:
                break
            if total and start >= total:
                break
            time.sleep(pause)
        log(f"Готово. Получено {fetched}, сохранено {stored}.")
        return {"fetched": fetched, "stored": stored, "total": total, "pages": pages,
                "error": error}
    finally:
        db.close()


def launch_run(cookie, body, **kw) -> bool:
    """Start a catalog pull in a background thread. False if one is already going."""
    with _LOCK:
        if _STATUS.get("running"):
            return False
        _STATUS.update(running=True, started=time.time(), total=0, fetched=0,
                       stored=0, msg="запуск…")

    def _bg():
        try:
            res = run_catalog(cookie, body, **kw)
            _STATUS["msg"] = (f"ошибка: {res['error']}" if res.get("error")
                              else f"готово: сохранено {res['stored']} из {res['fetched']}")
        except Exception as exc:  # noqa: BLE001
            _STATUS["msg"] = f"ошибка: {exc.__class__.__name__}: {exc}"
        finally:
            _STATUS["running"] = False

    threading.Thread(target=_bg, daemon=True).start()
    return True


# ---- read / filter / export -------------------------------------------------

_SORTS = {
    "sqi": DonorSite.sqi.desc(), "price": DonorSite.price_rur.asc(),
    "price_desc": DonorSite.price_rur.desc(), "dr": DonorSite.ahrefs_dr.desc(),
    "traffic": DonorSite.traffic.desc(), "ahrefs_traffic": DonorSite.ahrefs_traffic.desc(),
    "rating": DonorSite.rating.desc(), "spamness": DonorSite.spamness.asc(),
}


def regions(db) -> list[str]:
    rows = db.execute(
        select(DonorSite.region).distinct().where(DonorSite.region.is_not(None))
        .order_by(DonorSite.region)
    ).all()
    return [r for (r,) in rows if r]


def count(db) -> int:
    return db.execute(select(func.count()).select_from(DonorSite)).scalar_one()


def donor_rows(db, *, q=None, region=None, topic=None, min_sqi=None, max_price=None,
               min_dr=None, min_traffic=None, max_spamness=None, sort="sqi",
               limit: int | None = 500) -> list[DonorSite]:
    stmt = select(DonorSite)
    if q:
        stmt = stmt.where(DonorSite.domain.ilike(f"%{q}%"))
    if region:
        stmt = stmt.where(DonorSite.region == region)
    if topic:
        stmt = stmt.where(DonorSite.topics.ilike(f"%{topic}%"))
    if min_sqi:
        stmt = stmt.where(DonorSite.sqi >= min_sqi)
    if max_price:
        stmt = stmt.where(DonorSite.price_rur <= max_price)
    if min_dr:
        stmt = stmt.where(DonorSite.ahrefs_dr >= min_dr)
    if min_traffic:
        stmt = stmt.where(DonorSite.traffic >= min_traffic)
    if max_spamness is not None:
        stmt = stmt.where(DonorSite.spamness <= max_spamness)
    stmt = stmt.order_by(_SORTS.get(sort, _SORTS["sqi"]))
    if limit:
        stmt = stmt.limit(limit)
    return list(db.execute(stmt).scalars().all())


_EXPORT_COLS = [
    ("Домен", "domain"), ("URL", "site_url"), ("Название", "name"),
    ("ИКС", "sqi"), ("Ahrefs DR", "ahrefs_dr"), ("Moz DA", "da"),
    ("CF", "cf"), ("TF", "tf"), ("Трафик", "traffic"), ("Трафик (диап.)", "traffic_interval"),
    ("Ahrefs трафик", "ahrefs_traffic"), ("Спам Я", "spamness"),
    ("% индекс Я", "indexed_percent"), ("Стр. в индексе Я", "ya_indexed_count"),
    ("Цена ₽", "price_rur"), ("Цена $", "price_usd"), ("Написание ₽", "article_price_rur"),
    ("Регион", "region"), ("Тематика", "topics"), ("Язык", "lang"),
    ("Рейтинг", "rating"), ("Статей", "articles_count"), ("Эксклюзив", "is_exclusive"),
    ("Траст", "trusted"), ("Скрин", "screenshot"), ("Дата сбора", "captured_on"),
]


def build_export(rows: list[DonorSite], fmt: str = "csv"):
    data = []
    for r in rows:
        rec = {}
        for label, attr in _EXPORT_COLS:
            v = getattr(r, attr, None)
            rec[label] = v.isoformat() if hasattr(v, "isoformat") else v
        data.append(rec)
    df = pd.DataFrame(data, columns=[c[0] for c in _EXPORT_COLS])
    name = f"miralinks_donors_{date.today().isoformat()}"
    buf = io.BytesIO()
    if fmt == "csv":
        buf.write(df.to_csv(index=False).encode("utf-8-sig"))
        buf.seek(0)
        return f"{name}.csv", buf, CSV_MEDIA
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="Доноры", index=False)
    buf.seek(0)
    return f"{name}.xlsx", buf, XLSX_MEDIA
