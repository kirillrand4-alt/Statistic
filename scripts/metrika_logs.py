"""Yandex Metrica Logs API downloader: visits + hits, all fields, all domains.

List counters (id + domain):
    python scripts/metrika_logs.py --list

Rotated multi-domain sync (the main mode): downloads visits AND hits for every
site that has a counter. One Logs API request per domain is kept in flight — by
default for ALL domains at once (cap with --parallel) — each a --chunk-day
window (default 10), windows go newest -> oldest from --to (default yesterday).
Readiness is polled every --poll-sec (default 180 s). Transient API errors
(429 rate limit, 5xx, network) are retried — they don't abort the run. A window
still not ready after --timeout-min minutes is cancelled and skipped (it stays
a gap; re-run WITHOUT --force to download only what's missing). Counter per
domain is auto-detected (from existing visits, else by matching the domain in
--list); override with --targets. Same-domain twin properties (sc-domain: +
https://) collapse into one download target, and rows already downloaded under
such dupes are consolidated onto one site at startup (or run --merge-dupes).
    python scripts/metrika_logs.py --sync-all --force --from 2025-06-01
    python scripts/metrika_logs.py --sync-all                 # only-missing, last 365d
    python scripts/metrika_logs.py --sync-all --no-hits --parallel 3 --chunk 3

Single counter/site (visits or hits):
    python scripts/metrika_logs.py --counter 12345 --site 7 --from 2026-05-01 --to 2026-06-01 --source hits

Import a folder of files (.tsv/.csv/.txt, .gz, .zip):
    python scripts/metrika_logs.py --site 7 --import-dir /opt/seostat/uploads

Coverage / gaps for a site (visits and hits):
    python scripts/metrika_logs.py --site 7 --coverage

Reuses the stored Yandex token (the same y0_ token, must have metrika:read).
Run detached for long ranges:
    nohup .venv/bin/python scripts/metrika_logs.py --sync-all --force --from 2025-06-01 --to 2026-06-10 > /tmp/metrika.log 2>&1 &
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import tempfile
import time
from collections import deque
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.credentials import get_cred  # noqa: E402
from app.db.base import SessionLocal, init_db  # noqa: E402
from app.db.models import Hit, Visit  # noqa: E402
from app.services.visits import (  # noqa: E402
    HIT_FIELDS,
    VISIT_FIELDS,
    import_fileobj,
    import_hits,
    import_tsv,
)
from app.utils import domain_of  # noqa: E402

API = "https://api-metrika.yandex.net"
_SOURCE_FIELDS = {"visits": VISIT_FIELDS, "hits": HIT_FIELDS}
_MODEL = {"visits": Visit, "hits": Hit}


def _fields_for(source: str) -> str:
    return ",".join(_SOURCE_FIELDS.get(source, VISIT_FIELDS))


def _import_part(db, site_id: int, source: str, lines, update: bool) -> int:
    if source == "hits":
        return import_hits(db, site_id, lines, update=update)
    return import_tsv(db, site_id, lines, update=update)


def _token() -> str:
    t = (get_cred("yandex_metrika_token") or get_cred("yandex_wm_token")
         or get_settings().yandex_metrika_oauth_token)
    if not t:
        print("Нет токена. Подключите Яндекс (тот же токен) — нужен scope metrika:read.")
        sys.exit(1)
    return t


def _headers():
    return {"Authorization": f"OAuth {_token()}"}


def _fetch_counters() -> list[dict]:
    r = httpx.get(f"{API}/management/v1/counters", headers=_headers(),
                  params={"per_page": 500}, timeout=60)
    if r.status_code != 200:
        print("HTTP", r.status_code, r.text[:300])
        return []
    return r.json().get("counters", [])


def list_counters() -> None:
    for c in _fetch_counters():
        print(f"  id={c.get('id'):<10} {str(c.get('site')):<40} {c.get('name')}")


def _chunks(d1: date, d2: date, days: int, newest_first: bool = False):
    if newest_first:  # windows anchored to d2, yielded newest -> oldest
        cur = d2
        while cur >= d1:
            start = max(cur - timedelta(days=days - 1), d1)
            yield start, cur
            cur = start - timedelta(days=1)
        return
    cur = d1
    while cur <= d2:
        end = min(cur + timedelta(days=days - 1), d2)
        yield cur, end
        cur = end + timedelta(days=1)


def import_dir(site_id: int, path: str) -> int:
    """Import every visits file in a folder: .tsv/.csv/.txt, .gz, .zip."""
    init_db()
    db = SessionLocal()
    total = 0
    try:
        if os.path.isdir(path):
            files = sorted(
                os.path.join(r, f) for r, _, fs in os.walk(path) for f in fs
            )
        else:
            files = [path]
        if not files:
            print(f"В {path} файлов не найдено.")
            return 0
        for fp in files:
            base_name = os.path.basename(fp)
            if not fp.lower().endswith((".tsv", ".csv", ".txt", ".gz", ".zip")):
                print(f"  {base_name}: пропуск (не tsv/csv/txt/gz/zip)")
                continue
            try:
                with open(fp, "rb") as fh:
                    n = import_fileobj(db, site_id, fh, base_name)
                total += n
                print(f"  {base_name}: +{n} визитов", flush=True)
            except ValueError as exc:
                print(f"  {base_name}: пропуск — {exc}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"  {base_name}: ОШИБКА — {exc}", flush=True)
        print(f"Импортировано из {path}: {total} визитов")
        return total
    finally:
        db.close()


def _covered_dates(db, site_id: int, d1: date, d2: date, model=Visit) -> set[date]:
    """Dates that already have at least one row (visit/hit) for this site."""
    from sqlalchemy import func, select

    rows = db.execute(
        select(model.date).where(
            model.site_id == site_id, model.date >= d1, model.date <= d2
        ).group_by(model.date).having(func.count() > 0)
    ).all()
    return {r[0] for r in rows if r[0] is not None}


def coverage(site_id: int) -> None:
    """Print which dates have visits and where the gaps are."""
    from sqlalchemy import func, select

    init_db()
    db = SessionLocal()
    try:
        for label, model in (("визитов", Visit), ("хитов", Hit)):
            lo, hi, total = db.execute(
                select(func.min(model.date), func.max(model.date), func.count())
                .where(model.site_id == site_id)
            ).one()
            if not total:
                print(f"site {site_id}: {label} нет")
                continue
            have = _covered_dates(db, site_id, lo, hi, model)
            missing, cur = [], lo
            while cur <= hi:
                if cur not in have:
                    missing.append(cur)
                cur += timedelta(days=1)
            print(f"site {site_id}: {total} {label}, период {lo}..{hi}, "
                  f"дней с данными: {len(have)}, дыр: {len(missing)}")
            if missing:
                head = ", ".join(str(d) for d in missing[:15])
                more = f" … и ещё {len(missing) - 15}" if len(missing) > 15 else ""
                print(f"  пропущенные дни: {head}{more}")
    finally:
        db.close()


def _create_logrequest(counter: int, source: str, c1: date, c2: date):
    """Create a Logs API request.

    Returns the request id, the string ``"retry"`` on a transient failure
    (429 rate limit / 5xx / network error) or ``None`` on a permanent one.
    """
    try:
        r = httpx.post(
            f"{API}/management/v1/counter/{counter}/logrequests",
            headers=_headers(),
            params={"date1": c1.isoformat(), "date2": c2.isoformat(),
                    "fields": _fields_for(source), "source": source},
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001 — network hiccup, not fatal
        print(f"   create error ({exc.__class__.__name__}) — повторю позже")
        return "retry"
    if r.status_code in (200, 201):
        return r.json()["log_request"]["request_id"]
    if r.status_code == 429 or r.status_code >= 500:
        print(f"   create {r.status_code} (лимит/сбой API) — повторю позже")
        return "retry"
    print(f"   create FAILED {r.status_code}: {r.text[:200]}")
    return None


def _cancel_logrequest(base: str, req_id: int) -> None:
    try:
        httpx.post(f"{base}/{req_id}/cancel", headers=_headers(), timeout=60)
    except Exception:  # noqa: BLE001  (best effort — frees the counter's slot)
        pass


def _collect_request(db, base: str, req_id: int, site_id: int, source: str,
                     update: bool) -> int:
    """Download all parts of a processed request (streamed to disk), import, clean."""
    info = httpx.get(f"{base}/{req_id}", headers=_headers(), timeout=60).json()["log_request"]
    total = 0
    for p in info.get("parts", []):
        n = p.get("part_number", 0)
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", newline="",
                                         suffix=".tsv") as tf:
            with httpx.stream("GET", f"{base}/{req_id}/part/{n}/download",
                              headers=_headers(), timeout=600) as r:
                if r.status_code != 200:
                    print(f"   part {n} download FAILED {r.status_code}")
                    continue
                for textchunk in r.iter_text():
                    tf.write(textchunk)
            tf.flush()
            tf.seek(0)
            total += _import_part(db, site_id, source, tf, update)
    httpx.post(f"{base}/{req_id}/clean", headers=_headers(), timeout=60)
    return total


def download(counter: int, site_id: int, d1: date, d2: date, chunk: int, source: str,
             force: bool = False) -> None:
    init_db()
    db = SessionLocal()
    base = f"{API}/management/v1/counter/{counter}/logrequest"
    total = 0
    have = set() if force else _covered_dates(db, site_id, d1, d2, _MODEL.get(source, Visit))
    try:
        for c1, c2 in _chunks(d1, d2, chunk):
            days = {c1 + timedelta(days=i) for i in range((c2 - c1).days + 1)}
            if not force and days <= have:
                print(f"[{c1}..{c2}] уже в базе, пропускаю", flush=True)
                continue
            print(f"[{c1}..{c2}] {source}: создаю запрос...", flush=True)
            req_id = _create_logrequest(counter, source, c1, c2)
            for _ in range(30):  # rate limited — wait a minute and retry the window
                if req_id != "retry":
                    break
                time.sleep(60)
                req_id = _create_logrequest(counter, source, c1, c2)
            if not isinstance(req_id, int):
                continue
            status = "created"
            for _ in range(120):  # up to ~40 min
                time.sleep(20)
                status = httpx.get(f"{base}/{req_id}", headers=_headers(), timeout=60).json()["log_request"].get("status")
                if status in ("processed", "processed_with_errors", "canceled", "processing_failed"):
                    break
            if status not in ("processed", "processed_with_errors"):
                print(f"   не готово (status={status}), пропускаю")
                continue
            n = _collect_request(db, base, req_id, site_id, source, force)
            total += n
            print(f"   +{n} строк, очищено (request {req_id})")
        print(f"Готово. Импортировано строк: {total}")
    finally:
        db.close()


def _visit_counts(db, site_ids: list[int]) -> dict[int, int]:
    from sqlalchemy import func, select

    rows = db.execute(
        select(Visit.site_id, func.count()).where(Visit.site_id.in_(site_ids))
        .group_by(Visit.site_id)
    ).all()
    return dict(rows)


def resolve_targets(explicit: str | None = None, only_site: int | None = None):
    """Map DOMAINS -> Metrica counter. Returns (targets, skipped_labels).

    Same-domain sites (e.g. a GSC ``sc-domain:`` property and its ``https://``
    twin) collapse into ONE target — the site already holding the most visits
    (then the lowest id) — so one counter is never downloaded twice. Counter is
    taken from (1) explicit ``site:counter`` pairs (pinned sites always stay
    separate targets), (2) the most common counter_id among the site's existing
    visits, (3) a domain match against the account's counter list. Domains with
    no resolvable counter are skipped.
    """
    from sqlalchemy import func, select

    from app.db.models import Site

    init_db()
    db = SessionLocal()
    explicit_map = {}
    if explicit:
        for pair in explicit.split(","):
            if ":" in pair:
                sid, cid = pair.split(":", 1)
                explicit_map[int(sid.strip())] = int(cid.strip())
    targets, skipped, counters = [], [], None
    try:
        sites = db.execute(select(Site).where(Site.enabled.is_(True))).scalars().all()
        if only_site:
            sites = [s for s in sites if s.id == only_site]
        groups: dict[str, list] = {}
        for s in sites:
            groups.setdefault(domain_of(s.property_uri) or f"#site{s.id}", []).append(s)
        chosen = []  # (site, label)
        for dom, group in groups.items():
            pinned = [s for s in group if s.id in explicit_map]
            if pinned:  # explicit pins win and stay separate
                chosen += [(s, s.display_name or s.property_uri or f"site{s.id}")
                           for s in pinned]
                continue
            if len(group) == 1:
                s = group[0]
                chosen.append((s, s.display_name or s.property_uri or f"site{s.id}"))
                continue
            counts = _visit_counts(db, [s.id for s in group])
            canon = max(group, key=lambda s: (counts.get(s.id, 0), -s.id))
            print(f"Дубли домена {dom}: "
                  + ", ".join(f"site{s.id}" for s in group)
                  + f" → качаю в site{canon.id}")
            chosen.append((canon, dom))
        for s, label in chosen:
            counter = explicit_map.get(s.id)
            if counter is None:
                counter = db.execute(
                    select(Visit.counter_id)
                    .where(Visit.site_id == s.id, Visit.counter_id.isnot(None))
                    .group_by(Visit.counter_id).order_by(func.count().desc()).limit(1)
                ).scalar_one_or_none()
            if counter is None:
                if counters is None:
                    counters = _fetch_counters()
                host = domain_of(s.property_uri)
                for c in counters:
                    if host and domain_of(str(c.get("site") or "")) == host:
                        counter = c.get("id")
                        break
            if counter:
                targets.append((s.id, int(counter), label))
            else:
                skipped.append(label)
        return targets, skipped
    finally:
        db.close()


def merge_domain_dupes() -> int:
    """Consolidate already-downloaded rows of same-domain duplicate sites.

    Moves visits and hits from every duplicate onto the domain's canonical site
    (most visits, then lowest id); rows both sites have are kept once.
    Idempotent — a no-op once everything lives on one site. Returns rows moved.
    """
    from sqlalchemy import select

    from app.db.models import Site
    from app.services.visits import move_site_rows

    init_db()
    db = SessionLocal()
    moved = 0
    try:
        sites = db.execute(select(Site).where(Site.enabled.is_(True))).scalars().all()
        groups: dict[str, list] = {}
        for s in sites:
            dom = domain_of(s.property_uri)
            if dom:
                groups.setdefault(dom, []).append(s)
        for dom, group in groups.items():
            if len(group) < 2:
                continue
            counts = _visit_counts(db, [s.id for s in group])
            canon = max(group, key=lambda s: (counts.get(s.id, 0), -s.id))
            for s in group:
                if s.id == canon.id:
                    continue
                nv = move_site_rows(db, Visit, "visit_id", s.id, canon.id)
                nh = move_site_rows(db, Hit, "watch_id", s.id, canon.id)
                if nv or nh:
                    moved += nv + nh
                    print(f"[{dom}] site{s.id} → site{canon.id}: "
                          f"перенесено визитов {nv}, хитов {nh}", flush=True)
        return moved
    finally:
        db.close()


def sync_rotate(targets, d1: date, d2: date, chunk: int = 10, parallel: int | None = None,
                sources=("visits", "hits"), force: bool = True,
                timeout_min: int = 40, poll_sec: int = 180) -> None:
    """Download missing windows across many domains, one request per domain.

    Keeps one Logs API request in flight per domain — by default for *every*
    domain at once (cap with ``parallel``) — each a ``chunk``-day window, going
    newest -> oldest from ``d2``. Readiness is polled every ``poll_sec`` seconds;
    when a request is ready it's downloaded, imported and cleaned, and that
    domain's next (older) window starts. Transient API errors (429/5xx/network)
    are retried and never abort the run. A request still not ready after
    ``timeout_min`` minutes is cancelled and its window skipped (it stays a gap —
    re-run later without --force to download only what's missing).
    """
    init_db()
    db = SessionLocal()
    parallel = parallel or len(targets)
    chunks = list(_chunks(d1, d2, chunk, newest_first=True))
    queues: dict[str, deque] = {}
    meta: dict[str, tuple[int, int]] = {}  # label -> (site_id, counter)
    skipped = []
    try:
        for site_id, counter, label in targets:
            items = deque()
            for source in sources:
                have = set() if force else _covered_dates(db, site_id, d1, d2, _MODEL[source])
                for c1, c2 in chunks:
                    days = {c1 + timedelta(days=i) for i in range((c2 - c1).days + 1)}
                    if force or not (days <= have):
                        items.append((source, c1, c2, 0))  # 0 = create attempts
            if items:
                queues[label] = items
                meta[label] = (site_id, counter)
        if not queues:
            print("Нечего качать — за период всё уже покрыто.")
            return
        total_items = sum(len(q) for q in queues.values())
        print(f"К закачке: {total_items} запросов по {len(queues)} доменам · "
              f"параллельно {parallel} · окно {chunk} дн. (от новых к старым) · "
              f"период {d1}..{d2} · опрос раз в {poll_sec} с. · "
              f"таймаут {timeout_min} мин.", flush=True)

        inflight: dict[int, dict] = {}
        done = imported = 0
        while queues or inflight:
            busy = {v["label"] for v in inflight.values()}
            free = [lbl for lbl in queues if lbl not in busy]
            random.shuffle(free)
            while len(inflight) < parallel and free:
                lbl = free.pop()
                site_id, counter = meta[lbl]
                source, c1, c2, attempts = queues[lbl].popleft()
                if not queues[lbl]:
                    del queues[lbl]
                base = f"{API}/management/v1/counter/{counter}/logrequest"
                req_id = _create_logrequest(counter, source, c1, c2)
                if req_id == "retry":
                    # rate limited / API down — put the window back at the FRONT
                    # (order kept, no attempt burned) and pause creating
                    queues.setdefault(lbl, deque()).appendleft((source, c1, c2, attempts))
                    break
                if req_id is None:
                    if attempts < 2:  # bad response — retry the window a bit later
                        queues.setdefault(lbl, deque()).append((source, c1, c2, attempts + 1))
                    else:
                        skipped.append((lbl, source, c1, c2, "create failed"))
                    continue
                inflight[req_id] = dict(label=lbl, site_id=site_id, source=source,
                                        c1=c1, c2=c2, base=base, started=time.monotonic())
                print(f"[{lbl}] {source} {c1}..{c2}: запрос {req_id} "
                      f"(в работе {len(inflight)}/{parallel})", flush=True)
            if not inflight:
                time.sleep(poll_sec)  # nothing in flight (e.g. rate limited) — wait
                continue
            time.sleep(poll_sec)
            for req_id, it in list(inflight.items()):
                try:
                    status = httpx.get(f"{it['base']}/{req_id}", headers=_headers(),
                                       timeout=60).json()["log_request"].get("status")
                except Exception:  # noqa: BLE001
                    status = None
                age_min = (time.monotonic() - it["started"]) / 60.0
                if status in ("processed", "processed_with_errors"):
                    try:
                        n = _collect_request(db, it["base"], req_id, it["site_id"],
                                             it["source"], force)
                    except Exception as exc:  # noqa: BLE001 — 429/network: retry next poll
                        if age_min >= timeout_min:
                            _cancel_logrequest(it["base"], req_id)
                            del inflight[req_id]
                            skipped.append((it["label"], it["source"], it["c1"], it["c2"],
                                            "download failed"))
                            print(f"[{it['label']}] {it['source']} {it['c1']}..{it['c2']}: "
                                  f"скачивание так и не удалось — пропускаю окно")
                        else:
                            print(f"[{it['label']}] {it['source']} {it['c1']}..{it['c2']}: "
                                  f"скачивание не удалось ({exc.__class__.__name__}), "
                                  f"повторю через {poll_sec} с.", flush=True)
                        continue
                    imported += n
                    done += 1
                    del inflight[req_id]
                    print(f"[{it['label']}] {it['source']} {it['c1']}..{it['c2']}: "
                          f"+{n} строк, очищено · готово {done}/{total_items}, всего +{imported}",
                          flush=True)
                elif status in ("canceled", "processing_failed"):
                    del inflight[req_id]
                    skipped.append((it["label"], it["source"], it["c1"], it["c2"], status))
                    print(f"[{it['label']}] {it['source']} {it['c1']}..{it['c2']}: {status} — пропускаю")
                elif age_min >= timeout_min:
                    _cancel_logrequest(it["base"], req_id)
                    del inflight[req_id]
                    skipped.append((it["label"], it["source"], it["c1"], it["c2"], "timeout"))
                    print(f"[{it['label']}] {it['source']} {it['c1']}..{it['c2']}: "
                          f">{timeout_min} мин — отменил и пропустил окно")
        print(f"\nГотово. Импортировано строк: {imported}. Пропущено окон: {len(skipped)}.")
        for lbl, source, c1, c2, why in skipped[:40]:
            print(f"  ПРОПУЩЕНО [{lbl}] {source} {c1}..{c2} ({why})")
        if skipped:
            print("Пропущенные окна остались дырами — доберите позже по дням: "
                  "тот же запуск с --chunk 1 (без --force) добёрет только их.")
    finally:
        db.close()


def _auto_range(site_id: int) -> tuple[date, date] | None:
    """Default sync range: earliest visit date in the DB .. yesterday."""
    from sqlalchemy import func, select

    from app.db.models import Visit

    init_db()
    db = SessionLocal()
    try:
        lo = db.execute(select(func.min(Visit.date)).where(Visit.site_id == site_id)).scalar_one()
    finally:
        db.close()
    if lo is None:
        return None
    return lo, date.today() - timedelta(days=1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--import-dir", dest="import_dir")
    ap.add_argument("--sync-all", dest="sync_all", action="store_true",
                    help="качать по всем доменам, ротируя запросы в N потоков")
    ap.add_argument("--merge-dupes", dest="merge_dupes", action="store_true",
                    help="перенести визиты/хиты с дублей домена на один сайт и выйти")
    ap.add_argument("--targets", help='явная карта "site:counter,site:counter" (иначе авто)')
    ap.add_argument("--parallel", type=int, default=None,
                    help="одновременных запросов (по умолчанию — все домены сразу)")
    ap.add_argument("--no-hits", dest="no_hits", action="store_true", help="только визиты, без хитов")
    ap.add_argument("--poll-sec", dest="poll_sec", type=int, default=180,
                    help="как часто проверять готовность, сек. (по умолчанию 180 = 3 мин)")
    ap.add_argument("--timeout-min", dest="timeout_min", type=int, default=40)
    ap.add_argument("--counter", type=int)
    ap.add_argument("--site", type=int)
    ap.add_argument("--from", dest="d1")
    ap.add_argument("--to", dest="d2")
    ap.add_argument("--chunk", type=int, default=None)
    ap.add_argument("--source", default="visits")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    if a.list:
        list_counters(); return
    if a.merge_dupes:
        print(f"Готово, перенесено строк: {merge_domain_dupes()}")
        return
    if a.import_dir:
        if not a.site:
            print("Для --import-dir нужен --site."); return
        import_dir(a.site, a.import_dir)
    if a.coverage:
        if not a.site:
            print("Для --coverage нужен --site."); return
        coverage(a.site)
        if not (a.counter or a.sync_all):
            return

    if a.sync_all:
        sources = ("visits",) if a.no_hits else ("visits", "hits")
        chunk = a.chunk or 10
        merge_domain_dupes()  # consolidate old same-domain dupes (idempotent)
        targets, missed = resolve_targets(a.targets, a.site)
        if missed:
            print(f"Без счётчика (пропускаю): {', '.join(missed)}")
        if not targets:
            print("Не нашёл ни одного домена со счётчиком. "
                  "Задайте карту через --targets \"site:counter,...\" (id из --list).")
            return
        print("Домены → счётчики: " + ", ".join(f"{lbl}={c}" for _, c, lbl in targets))
        d2 = date.fromisoformat(a.d2) if a.d2 else date.today() - timedelta(days=1)
        d1 = date.fromisoformat(a.d1) if a.d1 else d2 - timedelta(days=365)
        sync_rotate(targets, d1, d2, chunk=chunk, parallel=a.parallel,
                    sources=sources, force=a.force, timeout_min=a.timeout_min,
                    poll_sec=a.poll_sec)
        return

    if not (a.counter and a.site):
        if not a.import_dir:
            print("Нужны --counter и --site (или --list / --coverage / --import-dir / --sync-all).")
        return

    if a.d1 and a.d2:
        d1, d2 = date.fromisoformat(a.d1), date.fromisoformat(a.d2)
    else:
        rng = _auto_range(a.site)
        if rng is None:
            print("В базе нет визитов этого сайта — задайте --from и --to явно.")
            return
        d1, d2 = rng
        print(f"Авто-диапазон: {d1}..{d2} (от самой ранней даты в базе до вчера)")
    download(a.counter, a.site, d1, d2, a.chunk or 1, a.source, force=a.force)


if __name__ == "__main__":
    main()
