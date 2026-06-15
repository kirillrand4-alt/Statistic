"""Yandex Metrica visits: import (Logs API TSV / uploaded file) + analysis."""
from __future__ import annotations

import csv
import gzip
import io
import json
import shutil
import tempfile
import zipfile
from datetime import date, datetime

from sqlalchemy import delete, func, literal, select
from sqlalchemy.orm import Session

from app.db.models import Hit, Visit
from app.providers.base import DateRange

# --- Visits (Logs API source=visits, ym:s:*) ---------------------------------
# Logs API field -> dedicated Visit column. Everything requested but not listed
# here is preserved in the JSON ``extra`` column.
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
_VISIT_INT = {"counter_id", "page_views", "duration", "bounce"}

# Full set of visit fields to REQUEST from the Logs API (a superset of the typed
# columns above; the rest is stored as JSON in ``extra``). Curated to fields that
# are valid for any counter — one invalid field makes the whole request fail.
VISIT_FIELDS = [
    "ym:s:visitID", "ym:s:counterID", "ym:s:watchIDs", "ym:s:date", "ym:s:dateTime",
    "ym:s:isNewUser", "ym:s:startURL", "ym:s:endURL", "ym:s:pageViews",
    "ym:s:visitDuration", "ym:s:bounce", "ym:s:ipAddress", "ym:s:regionCountry",
    "ym:s:regionCity", "ym:s:clientID", "ym:s:lastTrafficSource", "ym:s:lastAdvEngine",
    "ym:s:lastReferalSource", "ym:s:lastSearchEngine", "ym:s:lastSearchEngineRoot",
    "ym:s:lastSocialNetwork", "ym:s:lastSocialNetworkProfile", "ym:s:referer",
    "ym:s:lastUTMCampaign", "ym:s:lastUTMContent", "ym:s:lastUTMMedium",
    "ym:s:lastUTMSource", "ym:s:lastUTMTerm", "ym:s:browser", "ym:s:browserCountry",
    "ym:s:operatingSystem", "ym:s:operatingSystemRoot", "ym:s:deviceCategory",
    "ym:s:mobilePhone", "ym:s:mobilePhoneModel", "ym:s:screenWidth", "ym:s:screenHeight",
    "ym:s:screenColors", "ym:s:goalsID",
]

# --- Hits / pageviews (Logs API source=hits, ym:pv:*) ------------------------
HIT_FIELD_MAP = {
    "ym:pv:watchID": "watch_id",
    "ym:pv:counterID": "counter_id",
    "ym:pv:date": "date",
    "ym:pv:dateTime": "date_time",
    "ym:pv:URL": "url",
    "ym:pv:referer": "referer",
    "ym:pv:title": "title",
    "ym:pv:clientID": "client_id",
    "ym:pv:lastTrafficSource": "traffic_source",
    "ym:pv:lastSearchEngine": "search_engine",
    "ym:pv:lastAdvEngine": "adv_engine",
    "ym:pv:lastSocialNetwork": "social_network",
    "ym:pv:deviceCategory": "device",
    "ym:pv:operatingSystem": "os",
    "ym:pv:browser": "browser",
    "ym:pv:regionCountry": "region_country",
    "ym:pv:regionCity": "region_city",
    "ym:pv:ipAddress": "ip",
    "ym:pv:UTMSource": "utm_source",
    "ym:pv:UTMMedium": "utm_medium",
    "ym:pv:UTMCampaign": "utm_campaign",
    "ym:pv:UTMContent": "utm_content",
    "ym:pv:UTMTerm": "utm_term",
    "ym:pv:isPageView": "is_page_view",
    "ym:pv:download": "is_download",
    "ym:pv:link": "is_link",
    "ym:pv:notBounce": "not_bounce",
}
_HIT_INT = {"counter_id", "is_page_view", "is_download", "is_link", "not_bounce"}

HIT_FIELDS = [
    "ym:pv:watchID", "ym:pv:counterID", "ym:pv:date", "ym:pv:dateTime", "ym:pv:URL",
    "ym:pv:referer", "ym:pv:title", "ym:pv:clientID", "ym:pv:ipAddress",
    "ym:pv:regionCountry", "ym:pv:regionCity", "ym:pv:lastTrafficSource",
    "ym:pv:lastSearchEngine", "ym:pv:lastAdvEngine", "ym:pv:lastSocialNetwork",
    "ym:pv:browser", "ym:pv:operatingSystem", "ym:pv:deviceCategory",
    "ym:pv:UTMSource", "ym:pv:UTMMedium", "ym:pv:UTMCampaign", "ym:pv:UTMContent",
    "ym:pv:UTMTerm", "ym:pv:isPageView", "ym:pv:download", "ym:pv:link", "ym:pv:notBounce",
]


def _toint(v):
    """Parse an integer field. Try an exact int first (Metrica ids are 18-19
    digits — going through float would lose their low bits); fall back to float
    for values like "3.0", and to 0 for blanks/garbage."""
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0


# Signed 64-bit range — the limit of an INTEGER column. The visit/watch id (the
# key) is stored as TEXT so huge Metrica ids are kept; this only clamps the
# numeric value columns, so one stray oversized value can't crash the batch.
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1


def _todate(v):
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _insert_rows(db: Session, model, rows: list[dict], conflict_cols: list[str],
                 update: bool) -> int:
    """Bulk insert ``rows`` into ``model``; on conflict either ignore or update.

    ``update=True`` (re-download / refresh) overwrites every non-key column with
    the freshly downloaded value; ``update=False`` (gap-fill) keeps existing rows.
    """
    if not rows:
        return 0
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:  # pragma: no cover
        raise RuntimeError(f"Unsupported dialect {dialect!r}")
    stmt = insert(model).values(rows)
    if update:
        keys = set(conflict_cols)
        upd = {c: getattr(stmt.excluded, c) for c in rows[0] if c not in keys}
        stmt = stmt.on_conflict_do_update(index_elements=conflict_cols, set_=upd)
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=conflict_cols)
    db.execute(stmt)
    return len(rows)


def _import_rows(db: Session, site_id: int, lines, *, model, field_map: dict,
                 int_cols: set[str], key_col: str, what: str, update: bool) -> int:
    """Parse a Logs API TSV (tab-separated, header of ym:*:* fields) into ``model``.

    Mapped fields go to dedicated columns; every other requested field is kept in
    the JSON ``extra`` column, so no data is lost.
    """
    reader = csv.reader(lines, delimiter="\t")
    try:
        header = next(reader)
    except StopIteration:
        return 0
    short_map = {k.split(":")[-1]: v for k, v in field_map.items()}
    plan = []  # (index, target_column_or_None, short_name_for_extra)
    has_key = False
    for i, name in enumerate(header):
        short = name.split(":")[-1]
        col = field_map.get(name) or short_map.get(short)
        plan.append((i, col, short))
        if col == key_col:
            has_key = True
    if not has_key:
        raise ValueError(
            f"В файле нет столбца идентификатора {what} — это не та выгрузка Метрики."
        )

    all_cols = [c.name for c in model.__table__.columns if c.name != "id"]
    template = {c: None for c in all_cols}
    for c in int_cols:
        template[c] = 0
    conflict = ["site_id", key_col]

    written, payload = 0, []
    for row in reader:
        rec = dict(template)
        rec["site_id"] = site_id
        extra: dict[str, str] = {}
        for i, col, short in plan:
            if i >= len(row):
                continue
            val = row[i]
            if col:
                rec[col] = val
            elif val not in ("", None):
                extra[short] = val
        if not rec.get(key_col):  # the id (kept as text) is required
            continue
        for c in int_cols:  # numeric value columns; clamp so a stray huge value
            rec[c] = _toint(rec.get(c))  # can't crash the batch (key isn't here)
            if not (_INT64_MIN <= rec[c] <= _INT64_MAX):
                rec[c] = _INT64_MAX if rec[c] > 0 else _INT64_MIN
        if "date" in template:
            rec["date"] = _todate(rec.get("date"))
        for c in all_cols:
            if c not in int_cols and c != "date" and c != key_col and rec.get(c) == "":
                rec[c] = None
        rec["extra"] = json.dumps(extra, ensure_ascii=False) if extra else None
        payload.append(rec)
        if len(payload) >= 1000:
            written += _insert_rows(db, model, payload, conflict, update)
            payload = []
    if payload:
        written += _insert_rows(db, model, payload, conflict, update)
    db.commit()
    return written


def import_tsv(db: Session, site_id: int, lines, update: bool = False) -> int:
    """Import a Metrica *visits* TSV (ym:s:* fields) into the Visit table."""
    return _import_rows(db, site_id, lines, model=Visit, field_map=FIELD_MAP,
                        int_cols=_VISIT_INT, key_col="visit_id",
                        what="визита (ym:s:visitID)", update=update)


def import_hits(db: Session, site_id: int, lines, update: bool = False) -> int:
    """Import a Metrica *hits* TSV (ym:pv:* fields) into the Hit table."""
    return _import_rows(db, site_id, lines, model=Hit, field_map=HIT_FIELD_MAP,
                        int_cols=_HIT_INT, key_col="watch_id",
                        what="хита (ym:pv:watchID)", update=update)


def move_site_rows(db: Session, model, key_col: str, src_id: int, dst_id: int) -> int:
    """Re-point all of site ``src_id``'s rows (visits or hits) to site ``dst_id``.

    Set-based: copies the rows under ``dst_id`` with insert-or-ignore (rows the
    destination already has — same visit/watch id — keep the destination's
    version), then deletes the source rows. Returns rows removed from the source.
    Used to consolidate same-domain duplicate sites onto one canonical site.
    """
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:  # pragma: no cover
        raise RuntimeError(f"Unsupported dialect {dialect!r}")
    t = model.__table__
    cols = [c.name for c in t.columns if c.name != "id"]
    sel = select(*[literal(dst_id).label("site_id") if c == "site_id" else t.c[c]
                   for c in cols]).where(t.c.site_id == src_id)
    db.execute(insert(model).from_select(cols, sel)
               .on_conflict_do_nothing(index_elements=["site_id", key_col]))
    n = db.execute(delete(model).where(t.c.site_id == src_id)).rowcount or 0
    db.commit()
    return n


def import_fileobj(db: Session, site_id: int, fileobj, name: str) -> int:
    """Import one visits file from a binary file object, dispatching by extension.

    Handles ``.gz``, ``.zip`` (every member, non-visit members skipped) and plain
    ``.tsv/.csv/.txt`` (or unknown, treated as text). The object is read as a
    stream, so large uploads/archives don't need to fit in memory. A plain file
    without a visitID column raises ``ValueError``; inside a .zip such members are
    skipped so one stray file doesn't abort the whole archive.
    """
    low = (name or "").lower()
    if low.endswith(".gz"):
        with gzip.open(fileobj, "rt", encoding="utf-8", errors="ignore") as fh:
            return import_tsv(db, site_id, fh)
    if low.endswith(".zip"):
        # zip needs random access (its index is at the end). A Starlette upload is
        # a SpooledTemporaryFile, which on Python < 3.11 has no .seekable(); copy
        # such streams to a real temp file on disk before handing them to zipfile.
        try:
            seekable = fileobj.seekable()
        except AttributeError:
            seekable = False
        if seekable:
            return _import_zip(db, site_id, fileobj)
        with tempfile.TemporaryFile() as tmp:
            shutil.copyfileobj(fileobj, tmp)
            tmp.seek(0)
            return _import_zip(db, site_id, tmp)
    return import_tsv(db, site_id, io.TextIOWrapper(fileobj, encoding="utf-8", errors="ignore"))


def _import_zip(db: Session, site_id: int, zf) -> int:
    total = 0
    with zipfile.ZipFile(zf) as z:
        for entry in z.namelist():
            if entry.endswith("/"):
                continue
            with z.open(entry) as raw:
                try:
                    total += import_tsv(
                        db, site_id, io.TextIOWrapper(raw, encoding="utf-8", errors="ignore")
                    )
                except ValueError:
                    continue  # not a visits TSV — skip this archive member
    return total


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
