"""Push keywords to the ARSENKIN 'check-top' (ТОП-10) tool and store the SERP.

Source keywords from the DB (all collected queries, by domain or all) or a file,
batch them, run them through arsenkin for the chosen search engines, and save the
top-N URLs per keyword/engine into ``serp_result``. Respects arsenkin's limits:
<=5 concurrent tasks and <=30 requests/min (the client throttles + retries 429).

Token: stored credential ``arsenkin_token`` (set it in Настройки), or --token.

Examples:
    # preview cost for all keywords of a domain, Yandex + Google, Moscow:
    python scripts/arsenkin_top.py --domain prokompressor.ru --min-clicks 1
    # actually run it (detached for long lists):
    python scripts/arsenkin_top.py --domain prokompressor.ru --min-clicks 1 --apply
    # all domains, custom engines/regions, from a keyword file:
    python scripts/arsenkin_top.py --file kw.txt --se "2:213,11:1011969,3:213" --apply
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import deque
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.credentials import get_cred  # noqa: E402
from app.db.base import SessionLocal, init_db  # noqa: E402
from app.db.models import Site, Source  # noqa: E402
from app.providers.arsenkin import (  # noqa: E402
    DEFAULT_REGION,
    SE_LABELS,
    Arsenkin,
    is_done,
    parse_result,
)
from app.providers.base import DateRange  # noqa: E402
from app.services.keywords import keyword_rows  # noqa: E402
from app.utils import domain_of  # noqa: E402

SEARCH_ENGINES = ("gsc", "yandex_webmaster")


def _parse_se(raw: str | None) -> list[dict]:
    """'2:213,11:1011969' -> [{type:2,region:213},{type:11,region:1011969}].
    'type' alone uses the default Moscow region for that engine."""
    if not raw:
        return [{"type": 2, "region": 213}, {"type": 11, "region": 1011969}]  # Яндекс + Google
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            t, r = part.split(":", 1)
            t = int(t.strip())
            r = r.strip()
            out.append({"type": t, "region": int(r) if r.isdigit() else r})
        else:
            t = int(part)
            out.append({"type": t, "region": DEFAULT_REGION.get(t)})
    return out


def _keywords_from_db(domain: str | None, engines, dr: DateRange, min_clicks: int) -> list[str]:
    from sqlalchemy import select

    init_db()
    db = SessionLocal()
    try:
        sites = db.execute(
            select(Site.id, Site.property_uri, Source.code)
            .join(Source, Site.source_id == Source.id)
        ).all()
        ids = [sid for sid, uri, code in sites
               if code in engines and (not domain or domain_of(uri) == domain)]
        if not ids:
            return []
        return [r["query"] for r in keyword_rows(db, ids, dr, min_clicks=min_clicks, limit=None)
                if r["query"]]
    finally:
        db.close()


def _keywords_from_file(path: str) -> list[str]:
    seen, out = set(), []
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            for kw in line.replace("\t", ",").split(","):
                kw = kw.strip().strip('"')
                if kw and kw.lower() not in seen:
                    seen.add(kw.lower())
                    out.append(kw)
    return out


def _store(db, rows, task_id) -> int:
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
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        ins = pg_insert
    else:
        ins = sqlite_insert
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


def run(keywords, se, *, token, base, depth, snippets, batch, parallel, poll_sec,
        timeout_min, max_per_min) -> None:
    init_db()
    client = Arsenkin(token, base=base, max_per_min=max_per_min)
    db = SessionLocal()
    batches = deque(keywords[i:i + batch] for i in range(0, len(keywords), batch))
    total_batches = len(batches)
    se_label = ", ".join(f"{SE_LABELS.get(s['type'], s['type'])}({s.get('region')})" for s in se)
    print(f"К отправке: {len(keywords)} фраз × {len(se)} ПС [{se_label}] = "
          f"{len(keywords) * len(se)} лимитов · {total_batches} задач по {batch} фраз · "
          f"{parallel} параллельно · глубина {depth}", flush=True)

    inflight: dict = {}
    done_batches = stored = skipped = 0
    while batches or inflight:
        while len(inflight) < parallel and batches:
            b = list(batches.popleft())
            resp = client.set_task(b, se, depth=depth, is_snippet=snippets)
            tid = resp.get("task_id")
            if not tid:
                # queue full / limits / error — wait and requeue once at the back
                print(f"   set FAILED: {str(resp)[:200]} — верну в очередь", flush=True)
                batches.append(b)
                time.sleep(poll_sec)
                break
            inflight[tid] = {"batch": b, "started": time.monotonic()}
            print(f"[задача {tid}] {len(b)} фраз (в работе {len(inflight)}/{parallel})", flush=True)
        if not inflight:
            continue
        time.sleep(poll_sec)
        for tid, it in list(inflight.items()):
            payload = client.get(tid)
            if is_done(payload):
                n = _store(db, list(parse_result(payload)), tid)
                stored += n
                done_batches += 1
                del inflight[tid]
                print(f"[задача {tid}] готово · +{n} строк ТОП · готово {done_batches}/{total_batches}, "
                      f"всего +{stored}", flush=True)
            elif (time.monotonic() - it["started"]) / 60.0 >= timeout_min:
                del inflight[tid]
                skipped += len(it["batch"])
                print(f"[задача {tid}] >{timeout_min} мин — пропускаю ({len(it['batch'])} фраз)", flush=True)
    db.close()
    print(f"\nГотово. Строк ТОП сохранено: {stored}. Пропущено фраз: {skipped}.")


def main() -> None:
    ap = argparse.ArgumentParser()
    src = ap.add_argument_group("источник ключевых слов")
    src.add_argument("--domain", help='домен (напр. prokompressor.ru); пусто/all = все сайты')
    src.add_argument("--file", help="файл с фразами (по строке/через запятую) вместо БД")
    src.add_argument("--engines", default="gsc,yandex_webmaster",
                     help="источники запросов из БД: gsc,yandex_webmaster")
    src.add_argument("--from", dest="d1")
    src.add_argument("--to", dest="d2")
    src.add_argument("--min-clicks", dest="min_clicks", type=int, default=0)
    src.add_argument("--max-keywords", dest="max_keywords", type=int, default=0,
                     help="ограничить число фраз (0 = без ограничения)")
    api = ap.add_argument_group("arsenkin")
    api.add_argument("--token", help="API-токен (иначе из Настроек: arsenkin_token)")
    api.add_argument("--base", default="https://arsenkin.ru/api/tools")
    api.add_argument("--se", help='ПС: "type:region,..." (по умолч. 2:213,11:1011969 = Яндекс+Google Мск)')
    api.add_argument("--depth", type=int, default=10, help="глубина ТОП (5/10/20/30/50/100)")
    api.add_argument("--snippets", action="store_true", help="собирать сниппеты (title/описание)")
    api.add_argument("--batch", type=int, default=100, help="фраз в одной задаче")
    api.add_argument("--parallel", type=int, default=5, help="задач одновременно (макс. 5 у arsenkin)")
    api.add_argument("--poll-sec", dest="poll_sec", type=int, default=15)
    api.add_argument("--timeout-min", dest="timeout_min", type=int, default=30)
    api.add_argument("--rpm", type=int, default=28, help="запросов/мин (лимит arsenkin 30)")
    ap.add_argument("--apply", action="store_true", help="реально запустить (иначе только смета)")
    a = ap.parse_args()

    token = a.token or get_cred("arsenkin_token")
    if not token:
        print("Нет токена arsenkin. Вставьте его в Настройках (arsenkin_token) или --token.")
        sys.exit(1)
    se = _parse_se(a.se)

    if a.file:
        keywords = _keywords_from_file(a.file)
    else:
        d2 = date.fromisoformat(a.d2) if a.d2 else date.today() - timedelta(days=1)
        d1 = date.fromisoformat(a.d1) if a.d1 else d2 - timedelta(days=89)
        engines = [e.strip() for e in a.engines.split(",") if e.strip()]
        domain = None if (a.domain or "").lower() in ("", "all") else a.domain
        keywords = _keywords_from_db(domain, engines, DateRange(d1, d2), a.min_clicks)
    if a.max_keywords and len(keywords) > a.max_keywords:
        keywords = keywords[:a.max_keywords]
    if not keywords:
        print("Не нашёл ключевых слов по заданным условиям.")
        return

    se_label = ", ".join(f"{SE_LABELS.get(s['type'], s['type'])}({s.get('region')})" for s in se)
    cost = len(keywords) * len(se)
    print(f"Фраз: {len(keywords)} · ПС: {se_label} · ориентир. лимитов: {cost}")
    if not a.apply:
        print("Это предпросмотр (смета). Добавьте --apply, чтобы запустить парсинг ТОП.")
        return
    run(keywords, se, token=token, base=a.base, depth=a.depth, snippets=a.snippets,
        batch=a.batch, parallel=min(a.parallel, 5), poll_sec=a.poll_sec,
        timeout_min=a.timeout_min, max_per_min=a.rpm)


if __name__ == "__main__":
    main()
