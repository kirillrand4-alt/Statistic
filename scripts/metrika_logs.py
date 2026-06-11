"""Yandex Metrica visits: import a folder (tsv/gz/zip) and auto-fill the gaps.

List counters:
    python scripts/metrika_logs.py --list

Full sync (recommended): import every file in a folder (.tsv/.csv/.txt, .gz,
.zip), then detect missing days and download ONLY those via the Logs API
(create -> wait -> download parts -> clean at Yandex -> next):
    python scripts/metrika_logs.py --counter 12345 --site 7 --import-dir /opt/seostat/uploads

Without --from/--to the range is auto: earliest visit date in the DB .. yesterday.
Days already present in the DB are skipped (use --force to re-download).

Show coverage / gaps for a site:
    python scripts/metrika_logs.py --site 7 --coverage

Reuses the stored Yandex token (the same y0_ token, must have metrika:read).
Run detached for long ranges:
    nohup .venv/bin/python scripts/metrika_logs.py --counter 12345 --site 7 --import-dir /opt/seostat/uploads > /tmp/metrika.log 2>&1 &
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.credentials import get_cred  # noqa: E402
from app.db.base import SessionLocal, init_db  # noqa: E402
from app.services.visits import FIELD_MAP, import_tsv  # noqa: E402

API = "https://api-metrika.yandex.net"
FIELDS = ",".join(FIELD_MAP.keys())


def _token() -> str:
    t = (get_cred("yandex_metrika_token") or get_cred("yandex_wm_token")
         or get_settings().yandex_metrika_oauth_token)
    if not t:
        print("Нет токена. Подключите Яндекс (тот же токен) — нужен scope metrika:read.")
        sys.exit(1)
    return t


def _headers():
    return {"Authorization": f"OAuth {_token()}"}


def list_counters() -> None:
    r = httpx.get(f"{API}/management/v1/counters", headers=_headers(),
                  params={"per_page": 200}, timeout=60)
    if r.status_code != 200:
        print("HTTP", r.status_code, r.text[:300]); return
    for c in r.json().get("counters", []):
        print(f"  id={c.get('id'):<10} {str(c.get('site')):<40} {c.get('name')}")


def _chunks(d1: date, d2: date, days: int):
    cur = d1
    while cur <= d2:
        end = min(cur + timedelta(days=days - 1), d2)
        yield cur, end
        cur = end + timedelta(days=1)


def import_dir(site_id: int, path: str) -> int:
    """Import every visits file in a folder: .tsv/.csv/.txt, .gz, .zip."""
    import gzip
    import io
    import zipfile

    init_db()
    db = SessionLocal()
    total = 0

    def _one(name: str, fh) -> None:
        nonlocal total
        try:
            n = import_tsv(db, site_id, fh)
            total += n
            print(f"  {name}: +{n} визитов", flush=True)
        except ValueError as exc:
            print(f"  {name}: пропуск — {exc}", flush=True)

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
            low, base_name = fp.lower(), os.path.basename(fp)
            if low.endswith(".gz"):
                with gzip.open(fp, "rt", encoding="utf-8", errors="ignore") as fh:
                    _one(base_name, fh)
            elif low.endswith(".zip"):
                with zipfile.ZipFile(fp) as z:
                    for entry in z.namelist():
                        if entry.endswith("/"):
                            continue
                        with z.open(entry) as raw:
                            _one(f"{base_name}:{entry}",
                                 io.TextIOWrapper(raw, encoding="utf-8", errors="ignore"))
            elif low.endswith((".tsv", ".csv", ".txt")):
                with open(fp, encoding="utf-8", errors="ignore") as fh:
                    _one(base_name, fh)
            else:
                print(f"  {base_name}: пропуск (не tsv/csv/txt/gz/zip)")
        print(f"Импортировано из {path}: {total} визитов")
        return total
    finally:
        db.close()


def _covered_dates(db, site_id: int, d1: date, d2: date) -> set[date]:
    """Dates that already have at least one visit for this site."""
    from sqlalchemy import func, select

    from app.db.models import Visit

    rows = db.execute(
        select(Visit.date).where(
            Visit.site_id == site_id, Visit.date >= d1, Visit.date <= d2
        ).group_by(Visit.date).having(func.count() > 0)
    ).all()
    return {r[0] for r in rows if r[0] is not None}


def coverage(site_id: int) -> None:
    """Print which dates have visits and where the gaps are."""
    from sqlalchemy import func, select

    from app.db.models import Visit

    init_db()
    db = SessionLocal()
    try:
        lo, hi, total = db.execute(
            select(func.min(Visit.date), func.max(Visit.date), func.count()).where(Visit.site_id == site_id)
        ).one()
        if not total:
            print(f"site {site_id}: визитов нет")
            return
        have = _covered_dates(db, site_id, lo, hi)
        missing = []
        cur = lo
        while cur <= hi:
            if cur not in have:
                missing.append(cur)
            cur += timedelta(days=1)
        print(f"site {site_id}: {total} визитов, период {lo}..{hi}, дней с данными: {len(have)}, дыр: {len(missing)}")
        if missing:
            head = ", ".join(str(d) for d in missing[:15])
            more = f" … и ещё {len(missing) - 15}" if len(missing) > 15 else ""
            print(f"  пропущенные дни: {head}{more}")
    finally:
        db.close()


def download(counter: int, site_id: int, d1: date, d2: date, chunk: int, source: str,
             force: bool = False) -> None:
    init_db()
    db = SessionLocal()
    base = f"{API}/management/v1/counter/{counter}/logrequest"
    total = 0
    have = set() if force else _covered_dates(db, site_id, d1, d2)
    try:
        for c1, c2 in _chunks(d1, d2, chunk):
            days = {c1 + timedelta(days=i) for i in range((c2 - c1).days + 1)}
            if not force and days <= have:
                print(f"[{c1}..{c2}] уже в базе, пропускаю", flush=True)
                continue
            print(f"[{c1}..{c2}] создаю запрос...", flush=True)
            r = httpx.post(
                f"{API}/management/v1/counter/{counter}/logrequests",
                headers=_headers(),
                params={"date1": c1.isoformat(), "date2": c2.isoformat(), "fields": FIELDS, "source": source},
                timeout=60,
            )
            if r.status_code not in (200, 201):
                print(f"   create FAILED {r.status_code}: {r.text[:200]}")
                continue
            req_id = r.json()["log_request"]["request_id"]

            status = "created"
            for _ in range(120):  # up to ~40 min
                time.sleep(20)
                s = httpx.get(f"{base}/{req_id}", headers=_headers(), timeout=60).json()["log_request"]
                status = s.get("status")
                if status in ("processed", "processed_with_errors"):
                    break
                if status in ("canceled", "processing_failed"):
                    break
            if status not in ("processed", "processed_with_errors"):
                print(f"   не готово (status={status}), пропускаю")
                continue

            parts = httpx.get(f"{base}/{req_id}", headers=_headers(), timeout=60).json()["log_request"].get("parts", [])
            for p in parts:
                n = p.get("part_number", 0)
                dl = httpx.get(f"{base}/{req_id}/part/{n}/download", headers=_headers(), timeout=300)
                if dl.status_code == 200:
                    written = import_tsv(db, site_id, dl.text.splitlines())
                    total += written
                    print(f"   part {n}: +{written} визитов")
                else:
                    print(f"   part {n} download FAILED {dl.status_code}")
            httpx.post(f"{base}/{req_id}/clean", headers=_headers(), timeout=60)
            print(f"   очищено у Яндекса (request {req_id})")
        print(f"Готово. Импортировано визитов: {total}")
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
    ap.add_argument("--counter", type=int)
    ap.add_argument("--site", type=int)
    ap.add_argument("--from", dest="d1")
    ap.add_argument("--to", dest="d2")
    ap.add_argument("--chunk", type=int, default=1)
    ap.add_argument("--source", default="visits")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    if a.list:
        list_counters(); return
    if a.import_dir:
        if not a.site:
            print("Для --import-dir нужен --site."); return
        import_dir(a.site, a.import_dir)
    if a.coverage:
        if not a.site:
            print("Для --coverage нужен --site."); return
        coverage(a.site)
        if not a.counter:
            return
    if not (a.counter and a.site):
        if not a.import_dir:
            print("Нужны --counter и --site (или --list / --coverage / --import-dir).")
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
    download(a.counter, a.site, d1, d2, a.chunk, a.source, force=a.force)


if __name__ == "__main__":
    main()
