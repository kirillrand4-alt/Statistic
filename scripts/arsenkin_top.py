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
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.credentials import get_cred  # noqa: E402
from app.db.base import SessionLocal, init_db  # noqa: E402
from app.db.models import Site, Source  # noqa: E402
from app.providers.arsenkin import (  # noqa: E402
    DEFAULT_REGION,
    SE_LABELS,
    Arsenkin,
    check_done,
    parse_result,
)
from app.providers.base import DateRange  # noqa: E402
from app.services.keywords import keyword_rows  # noqa: E402
from app.services.serp_run import run_top10  # noqa: E402
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


def dump(path: str, captured_on=None, only_domain: str | None = None,
         se_types: list[int] | None = None) -> None:
    """Write stored SERP results to a CSV (long format: one row per position)."""
    import csv

    from sqlalchemy import func, select

    from app.db.models import SerpResult

    init_db()
    db = SessionLocal()
    try:
        cap = (date.fromisoformat(captured_on) if captured_on
               else db.execute(select(func.max(SerpResult.captured_on))).scalar())
        if cap is None:
            print("В базе нет результатов ТОП (serp_result пуст). Сначала запусти парсинг.")
            return
        stmt = select(SerpResult).where(SerpResult.captured_on == cap)
        if se_types:
            stmt = stmt.where(SerpResult.se.in_(se_types))
        if only_domain:
            stmt = stmt.where(SerpResult.url_domain == only_domain)
        stmt = stmt.order_by(SerpResult.keyword, SerpResult.se, SerpResult.position)
        rows = db.execute(stmt).scalars().all()
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["Запрос", "ПС", "Регион", "Позиция", "URL", "Домен", "Заголовок", "Дата"])
            for r in rows:
                w.writerow([r.keyword, SE_LABELS.get(r.se, r.se), r.region, r.position,
                            r.url, r.url_domain, r.title, r.captured_on])
        print(f"Выгружено строк: {len(rows)} → {path} (срез {cap}"
              + (f", домен {only_domain}" if only_domain else "") + ")")
    finally:
        db.close()


def run(keywords, se, *, token, base, depth, snippets, batch, parallel, poll_sec,
        timeout_min, max_per_min) -> None:
    se_label = ", ".join(f"{SE_LABELS.get(s['type'], s['type'])}({s.get('region')})" for s in se)
    print(f"К отправке: {len(keywords)} фраз × {len(se)} ПС [{se_label}] = "
          f"{len(keywords) * len(se)} лимитов · по {batch} фраз · ≤{min(parallel, 5)} параллельно · "
          f"глубина {depth}", flush=True)
    res = run_top10(keywords, se, token=token, base=base, depth=depth, snippets=snippets,
                    batch=batch, parallel=parallel, poll_sec=poll_sec, timeout_min=timeout_min,
                    max_per_min=max_per_min, log=lambda m: print(m, flush=True))
    print(f"\nГотово. Строк ТОП сохранено: {res['stored']}. Пропущено фраз: {res['skipped']}.")


def probe(phrase: str, se, *, token, base, depth, snippets, poll_sec=10, timeout_min=10) -> None:
    """Run ONE phrase end-to-end: print the raw set, then poll /check (printing
    its raw response each time) and, once it reports done, /get + parsed rows.
    For verifying the live API (token, fields, readiness, result shape)."""
    import time as _t

    client = Arsenkin(token, base=base)
    resp = client.set_task([phrase], se, depth=depth, is_snippet=snippets)
    print("set ->", resp)
    tid = resp.get("task_id")
    if not tid:
        print("Нет task_id — проверь токен/тариф/доступ (ответ выше).")
        return
    deadline = _t.monotonic() + timeout_min * 60
    while _t.monotonic() < deadline:
        _t.sleep(poll_sec)
        chk = client.check(tid)
        print("check ->", str(chk)[:200])
        if check_done(chk):
            rows = list(parse_result(client.get(tid)))
            print(f"get -> готово, распарсено строк: {len(rows)}")
            for r in rows[:20]:
                print(f"  se={r['se']} #{r['position']}  {r['url']}  | {(r.get('title') or '')[:60]}")
            return
    print("Не дождался завершения за лимит времени (увеличь --timeout-min).")


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
    api.add_argument("--probe", help="проверить ОДНУ фразу и показать сырой ответ (диагностика API)")
    ap.add_argument("--apply", action="store_true", help="реально запустить (иначе только смета)")
    dmp = ap.add_argument_group("выгрузка из базы")
    dmp.add_argument("--dump", help="выгрузить сохранённый ТОП в CSV по этому пути и выйти")
    dmp.add_argument("--captured-on", dest="captured_on", help="дата среза (по умолч. последняя)")
    dmp.add_argument("--only-domain", dest="only_domain", help="только строки этого домена (свои позиции)")
    a = ap.parse_args()

    if a.dump:
        se_types = [s["type"] for s in _parse_se(a.se)] if a.se else None
        dump(a.dump, captured_on=a.captured_on, only_domain=a.only_domain, se_types=se_types)
        return

    token = a.token or get_cred("arsenkin_token")
    if not token:
        print("Нет токена arsenkin. Вставьте его в Настройках (arsenkin_token) или --token.")
        sys.exit(1)
    se = _parse_se(a.se)

    if a.probe:
        probe(a.probe, se, token=token, base=a.base, depth=a.depth, snippets=a.snippets)
        return

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
