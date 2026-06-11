"""Download Yandex Metrica visits via the Logs API in short chunks and import.

List counters:
    python scripts/metrika_logs.py --list

Download visits for a counter into a site (chunked: create -> wait -> download
parts -> clean -> next):
    python scripts/metrika_logs.py --counter 12345 --site 7 --from 2026-05-01 --to 2026-06-01 --chunk 1

Reuses the stored Yandex token (the same y0_ token, must have metrika:read).
Run detached for long ranges:
    nohup .venv/bin/python scripts/metrika_logs.py --counter 12345 --site 7 --from 2025-06-01 --to 2026-06-01 > /tmp/metrika.log 2>&1 &
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


def download(counter: int, site_id: int, d1: date, d2: date, chunk: int, source: str) -> None:
    init_db()
    db = SessionLocal()
    base = f"{API}/management/v1/counter/{counter}/logrequest"
    total = 0
    try:
        for c1, c2 in _chunks(d1, d2, chunk):
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--counter", type=int)
    ap.add_argument("--site", type=int)
    ap.add_argument("--from", dest="d1")
    ap.add_argument("--to", dest="d2")
    ap.add_argument("--chunk", type=int, default=1)
    ap.add_argument("--source", default="visits")
    a = ap.parse_args()
    if a.list:
        list_counters(); return
    if not (a.counter and a.site and a.d1 and a.d2):
        print("Нужны --counter, --site, --from, --to (или --list).")
        return
    download(a.counter, a.site, date.fromisoformat(a.d1), date.fromisoformat(a.d2), a.chunk, a.source)


if __name__ == "__main__":
    main()
