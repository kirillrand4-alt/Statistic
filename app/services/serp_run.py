"""Run the ARSENKIN check-top flow (shared by the CLI and the web button).

Keeps the documented limits: <=5 tasks in flight, <=30 req/min (the client
throttles + retries 429), queue never overfilled (we only hold <=parallel
tasks at arsenkin at once). A module-level status lets the web show progress.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import date

from app.db.base import SessionLocal, init_db
from app.providers.arsenkin import SE_LABELS, Arsenkin, is_done, parse_result
from app.utils import domain_of

DEFAULT_BASE = "https://arsenkin.ru/api/tools"

# Live progress of the (single) web-launched run.
_STATUS: dict = {"running": False, "total": 0, "done": 0, "stored": 0,
                 "started": 0.0, "msg": ""}
_LOCK = threading.Lock()


def current_status() -> dict:
    return dict(_STATUS)


def store_rows(db, rows, task_id) -> int:
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from app.db.models import SerpResult

    today = date.today()
    payload = []
    for r in rows:
        if not r.get("query") or r.get("se") is None:
            continue
        payload.append({
            "keyword": r["query"], "se": int(r["se"]),
            "region": r.get("region") if isinstance(r.get("region"), int) else None,
            "position": int(r["position"]), "url": r.get("url"),
            "url_domain": domain_of(r.get("url") or "") or None,
            "title": r.get("title"), "captured_on": today, "task_id": str(task_id),
        })
    if not payload:
        return 0
    ins = sqlite_insert
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        ins = pg_insert
    for i in range(0, len(payload), 500):
        chunk = payload[i:i + 500]
        stmt = ins(SerpResult).values(chunk)
        upd = {c: getattr(stmt.excluded, c) for c in chunk[0]
               if c not in ("keyword", "se", "region", "position", "captured_on")}
        stmt = stmt.on_conflict_do_update(
            index_elements=["keyword", "se", "region", "position", "captured_on"], set_=upd)
        db.execute(stmt)
    db.commit()
    return len(payload)


def run_top10(keywords, se, *, token, base=DEFAULT_BASE, depth=10, snippets=False,
              batch=100, parallel=5, poll_sec=15, timeout_min=30, max_per_min=28,
              log=lambda *_: None) -> dict:
    """Submit ``keywords`` in batches, poll, store the SERP. Updates _STATUS."""
    init_db()
    client = Arsenkin(token, base=base, max_per_min=max_per_min)
    db = SessionLocal()
    batches = deque(list(keywords[i:i + batch]) for i in range(0, len(keywords), batch))
    total = len(batches)
    _STATUS.update(total=total, done=0, stored=0)
    inflight: dict = {}
    done = stored = skipped = 0
    try:
        while batches or inflight:
            while len(inflight) < min(parallel, 5) and batches:
                b = batches.popleft()
                resp = client.set_task(b, se, depth=depth, is_snippet=snippets)
                tid = resp.get("task_id")
                if not tid:
                    log(f"   set FAILED: {str(resp)[:200]} — верну в очередь")
                    batches.append(b)
                    time.sleep(poll_sec)
                    break
                inflight[tid] = {"batch": b, "started": time.monotonic()}
                log(f"[задача {tid}] {len(b)} фраз (в работе {len(inflight)}/{min(parallel, 5)})")
            if not inflight:
                continue
            time.sleep(poll_sec)
            for tid, it in list(inflight.items()):
                payload = client.get(tid)
                if is_done(payload):
                    n = store_rows(db, list(parse_result(payload)), tid)
                    stored += n
                    done += 1
                    del inflight[tid]
                    _STATUS.update(done=done, stored=stored)
                    log(f"[задача {tid}] готово · +{n} строк · {done}/{total}, всего +{stored}")
                elif (time.monotonic() - it["started"]) / 60.0 >= timeout_min:
                    del inflight[tid]
                    skipped += len(it["batch"])
                    log(f"[задача {tid}] >{timeout_min} мин — пропускаю")
        log(f"Готово. Строк ТОП: {stored}. Пропущено фраз: {skipped}.")
        return {"stored": stored, "skipped": skipped, "batches": total}
    finally:
        db.close()


def launch_run(keywords, se, **kw) -> bool:
    """Start a run in a background thread. Returns False if one is already going."""
    with _LOCK:
        if _STATUS.get("running"):
            return False
        _STATUS.update(running=True, started=time.time(), total=0, done=0, stored=0,
                       msg=f"{len(keywords)} фраз × {len(se)} ПС")

    def _bg():
        try:
            run_top10(keywords, se, **kw)
            _STATUS["msg"] = f"готово: сохранено {_STATUS['stored']} строк"
        except Exception as exc:  # noqa: BLE001
            _STATUS["msg"] = f"ошибка: {exc.__class__.__name__}: {exc}"
        finally:
            _STATUS["running"] = False

    threading.Thread(target=_bg, daemon=True).start()
    return True
