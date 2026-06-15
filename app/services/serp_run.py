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
from app.providers.arsenkin import Arsenkin, check_done, parse_result
from app.utils import domain_of

DEFAULT_BASE = "https://arsenkin.ru/api/tools"

# Live progress of the (single) web-launched run.
_STATUS: dict = {"running": False, "total": 0, "done": 0, "stored": 0,
                 "started": 0.0, "msg": ""}
_LOCK = threading.Lock()


def current_status() -> dict:
    return dict(_STATUS)


def _ins(db):
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        return pg_insert
    return sqlite_insert


def record_task(db, task_id, phrases: int) -> None:
    """Persist a submitted task as pending (insert-or-ignore) so it can be
    recovered after a crash/restart."""
    from app.db.models import SerpTask
    stmt = _ins(db)(SerpTask).values(task_id=str(task_id), status="pending", phrases=int(phrases))
    db.execute(stmt.on_conflict_do_nothing(index_elements=["task_id"]))
    db.commit()


def mark_task_done(db, task_id, stored: int) -> None:
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.db.models import SerpTask
    row = db.execute(select(SerpTask).where(SerpTask.task_id == str(task_id))).scalar_one_or_none()
    if row is not None:
        row.status, row.stored = "done", int(stored)
        row.finished_at = datetime.now(timezone.utc)
        db.commit()


def pending_count(db) -> int:
    from sqlalchemy import func, select

    from app.db.models import SerpTask
    return int(db.execute(
        select(func.count()).select_from(SerpTask).where(SerpTask.status != "done")
    ).scalar_one())


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
            "title": r.get("title"), "snippet": r.get("snippet"),
            "captured_on": today, "task_id": str(task_id),
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
    """Submit ``keywords`` in batches, poll ``/check`` for completion, then fetch
    and store the SERP. Updates _STATUS. Readiness comes from /check (not /get),
    because /get can report TASK_RESULT before the SERP is actually collected."""
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
                record_task(db, tid, len(b))  # persist so an interrupted run can resume
                log(f"[задача {tid}] {len(b)} фраз (в работе {len(inflight)}/{min(parallel, 5)})")
            if not inflight:
                continue
            time.sleep(poll_sec)
            for tid, it in list(inflight.items()):
                age = time.monotonic() - it["started"]
                if check_done(client.check(tid)):  # ready per /check -> fetch result
                    n = store_rows(db, list(parse_result(client.get(tid))), tid)
                    mark_task_done(db, tid, n)
                    stored += n
                    done += 1
                    del inflight[tid]
                    _STATUS.update(done=done, stored=stored)
                    log(f"[задача {tid}] готово · +{n} строк · {done}/{total}, всего +{stored}")
                elif age / 60.0 >= timeout_min:
                    del inflight[tid]
                    skipped += len(it["batch"])
                    log(f"[задача {tid}] >{timeout_min} мин — пропускаю")
                # else: ещё выполняется — ждём следующего опроса
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


def fetch_pending(*, token, base=DEFAULT_BASE, max_per_min=28, log=lambda *_: None) -> dict:
    """Re-check every still-pending task and store the ones arsenkin has finished.
    Recovers a web run that was interrupted (restart/network) — no extra cost."""
    from sqlalchemy import select

    from app.db.models import SerpTask
    init_db()
    client = Arsenkin(token, base=base, max_per_min=max_per_min)
    db = SessionLocal()
    done = stored = 0
    try:
        tasks = db.execute(
            select(SerpTask).where(SerpTask.status != "done").order_by(SerpTask.id)
        ).scalars().all()
        total = len(tasks)
        _STATUS.update(total=total, done=0, stored=0)
        for t in tasks:
            if check_done(client.check(t.task_id)):
                n = store_rows(db, list(parse_result(client.get(t.task_id))), t.task_id)
                mark_task_done(db, t.task_id, n)
                stored += n
                done += 1
                _STATUS.update(done=done, stored=stored)
                log(f"[задача {t.task_id}] готово · +{n} строк ({done}/{total})")
            else:
                log(f"[задача {t.task_id}] ещё не готова — оставляю в очереди")
        log(f"Готово. Догружено задач: {done}/{total}, строк: {stored}.")
        return {"checked": total, "done": done, "stored": stored, "pending": total - done}
    finally:
        db.close()


def launch_fetch_pending(**kw) -> bool:
    """Run :func:`fetch_pending` in a background thread (shares the run status)."""
    with _LOCK:
        if _STATUS.get("running"):
            return False
        _STATUS.update(running=True, started=time.time(), total=0, done=0, stored=0,
                       msg="догрузка недостающих…")

    def _bg():
        try:
            res = fetch_pending(**kw)
            _STATUS["msg"] = (f"догружено: {res['done']} задач, {res['stored']} строк"
                              + (f" (осталось {res['pending']})" if res["pending"] else ""))
        except Exception as exc:  # noqa: BLE001
            _STATUS["msg"] = f"ошибка: {exc.__class__.__name__}: {exc}"
        finally:
            _STATUS["running"] = False

    threading.Thread(target=_bg, daemon=True).start()
    return True
