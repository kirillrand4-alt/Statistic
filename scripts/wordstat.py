"""Wordstat history scraper — STEP 1: log in once and discover the internal
"История запросов" endpoint (the JSON the page loads for the dynamics graph).

Reuses the same logged-in Yandex profile as the Webvisor scraper, so login is
shared. Run --discover, open «История» for any phrase in the window, and this
captures the XHR/fetch JSON responses for inspection — then we build the actual
fetcher (per-phrase history) + captcha hook + storage on top.

  python scripts/wordstat.py --login                 # log into Yandex (headed)
  python scripts/wordstat.py --discover "компрессор"  # capture the history XHR
  python scripts/wordstat.py --collect --from-db      # collect history for our queries
  python scripts/wordstat.py --status                 # show what's already collected

Captcha: by default you solve any Yandex SmartCaptcha by hand in the window.
For unattended runs configure a cloud solver once, then pass --captcha auto:
  python scripts/wordstat.py --set-captcha YOUR_KEY --captcha-provider capmonster
  python scripts/wordstat.py --collect --from-db --captcha auto
(Yandex SmartCaptcha is confirmed on 2captcha; CapMonster support is not
guaranteed — switch --captcha-provider 2captcha if it refuses.)

NOTE: scraping Wordstat is against Yandex ToS and may hit Yandex SmartCaptcha —
keep request rates low; this tool is for your own keyword research.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)  # so `import app.*` works when run as a script
DATA_DIR = os.environ.get("WORDSTAT_DATA", os.path.join(REPO, "data", "wordstat"))
# Reuse the Webvisor Yandex login by default (same account/cookies).
PROFILE_DIR = os.environ.get(
    "WORDSTAT_PROFILE", os.path.join(REPO, "data", "webvisor", "profile"))
DEBUG_DIR = os.path.join(DATA_DIR, "debug")
WORDSTAT_URL = os.environ.get("WORDSTAT_URL", "https://wordstat.yandex.ru/")
VIEWPORT = {"width": 1440, "height": 900}
# JSON whose body contains these is likely the history series (refined after discover).
NEEDLES = ("graph", "history", "dynamics", "byMonth", "byWeek", "period", "shows", "absolute")


def _pw():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright не установлен: .venv/bin/pip install playwright "
                 "&& .venv/bin/playwright install --with-deps chromium")
    return sync_playwright


def cmd_login() -> None:
    os.makedirs(PROFILE_DIR, exist_ok=True)
    with _pw()() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=False, viewport=VIEWPORT)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(WORDSTAT_URL, wait_until="load")
        print("Открыт Wordstat. Войди в Яндекс в окне, открой любую фразу и вкладку «История».")
        try:
            input("Когда готов — нажми Enter, чтобы сохранить сессию и закрыть… ")
        except EOFError:
            time.sleep(180)
        ctx.close()
    print(f"Профиль сохранён в {PROFILE_DIR}. Дальше: --discover \"фраза\"")


def cmd_discover(query: str) -> None:
    """Open Wordstat for ``query`` headed and capture JSON responses. Switch the
    window to «История» manually — we record every JSON XHR so we can identify
    the history endpoint (URL, params, shape)."""
    os.makedirs(DEBUG_DIR, exist_ok=True)
    grabbed: list[tuple] = []
    seen: list[str] = []

    def on_resp(resp):
        try:
            url = resp.url
            seen.append(url)
            ct = (resp.headers.get("content-type", "") or "").lower()
            is_api = "/wordstat/api/" in url
            if "json" not in ct and not is_api:
                return
            body = resp.text()
        except Exception:
            return
        # request payload + key headers — needed to replay the endpoint later
        try:
            req = resp.request
            method, post = req.method, (req.post_data or "")
            hdrs = {k: v for k, v in (req.headers or {}).items()
                    if k.lower() in ("content-type", "x-csrf-token", "x-requested-with", "referer")}
        except Exception:
            method, post, hdrs = "?", "", {}
        score = sum(n in body for n in NEEDLES) + (2 if (query and query in body) else 0)
        if is_api or score:
            grabbed.append((100 if is_api else score, url, method, post, hdrs, body))

    with _pw()() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=False, viewport=VIEWPORT)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", on_resp)
        page.goto(WORDSTAT_URL, wait_until="domcontentloaded", timeout=60000)
        print(f"Открыт Wordstat. Введи в окне фразу «{query}», переключись на «История».")
        print("Жду 90 сек (или нажми Enter раньше)…")
        try:
            page.wait_for_timeout(2000)
            input()
        except EOFError:
            page.wait_for_timeout(90000)
        if "passport" in page.url or "auth" in page.url:
            print("  ⚠ не залогинен — сначала: --login")
        ctx.close()

    grabbed.sort(key=lambda x: x[0], reverse=True)
    print(f"\nJSON-ответов просмотрено: {len(seen)}; пойманных: {len(grabbed)}")
    for i, (score, url, method, post, hdrs, body) in enumerate(grabbed[:8]):
        path = os.path.join(DEBUG_DIR, f"ws_{i}.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        with open(path + ".req.txt", "w", encoding="utf-8") as f:
            f.write(f"{method} {url}\n\nheaders: {json.dumps(hdrs, ensure_ascii=False)}\n\nbody:\n{post}")
        print(f"\n[{i}] score={score} {method} {url[:150]}")
        print(f"   запрос(body): {post[:300]}")
        print(f"   ответ: {body[:300]}")
        print(f"   сохранил: {path} (+ .req.txt)")
    if not grabbed:
        print("Не поймал данные истории. Все JSON-URL (последние 25):")
        for u in seen[-25:]:
            print("  ", u)
    print("\nПришли сюда вывод и/или файлы data/wordstat/debug/ws_*.json — добью загрузчик.")


GRAPH_URL = "https://wordstat.yandex.ru/wordstat/api/getGraph"


def _month_floor(ddmmyyyy: str):
    """'dd.mm.YYYY' -> date of the 1st of that month (matches how rows are stored)."""
    from datetime import datetime
    return datetime.strptime(ddmmyyyy, "%d.%m.%Y").date().replace(day=1)


def _done_hashes(db, region, dev, lo_month, hi_month) -> set:
    """query_hash set already having ANY data for region/device within the window —
    these phrases are fully collected (each phrase is saved atomically), so a resumed
    run can skip them."""
    from sqlalchemy import distinct, select

    from app.db.models import WordstatHistory as W
    rows = db.execute(
        select(distinct(W.query_hash)).where(
            W.region == region, W.device == dev,
            W.date >= lo_month, W.date <= hi_month)
    ).all()
    return {h for (h,) in rows}


def _default_range() -> tuple[str, str]:
    """24-month window ending at the previous full month (dd.mm.YYYY) — Wordstat max."""
    from datetime import date, timedelta
    today = date.today()
    end = today.replace(day=1) - timedelta(days=1)  # last day of previous month
    sy, sm = end.year, end.month - 23
    while sm <= 0:
        sm += 12
        sy -= 1
    start = date(sy, sm, 1)
    return start.strftime("%d.%m.%Y"), end.strftime("%d.%m.%Y")


def _payload(phrase, region, device, d_from, d_to) -> dict:
    return {
        "currentDevice": device, "currentGraphType": "month", "dbname": "rus",
        "filters": {"region": region, "tableType": "popular"},
        "searchValue": phrase, "startDate": d_from, "endDate": d_to,
        "text": {"graph": {"title": "", "disclaimer": ""},
                 "map": {"title": "", "disclaimer": ""},
                 "table": {"title": "", "disclaimer": ""}},
    }


def _parse_graph(resp):
    """-> ('ok', rows) | ('captcha', None) | ('empty', []) | ('error', msg).

    ``empty`` — Wordstat ответил нормально, но истории по фразе нет (низкочастотный
    хвост) — это не сбой. ``error`` — ответ есть, но структура неожиданная (msg —
    причина). ``captcha`` — редирект на проверку/блок по частоте запросов.
    """
    try:
        txt = resp.text()
    except Exception as e:  # noqa: BLE001
        return "error", f"нет ответа: {str(e)[:80]}"
    low = txt.lower()
    if "showcaptcha" in low or "smartcaptcha" in low or "checkcaptcha" in low:
        return "captcha", None
    if resp.status != 200:
        return ("captcha", None) if resp.status in (403, 429) else ("error", f"HTTP {resp.status}")
    if "captcha" in low and '"graph"' not in txt:
        return "captcha", None
    try:
        data = json.loads(txt)
    except Exception:  # noqa: BLE001
        return "error", "ответ не JSON"
    try:
        from datetime import date
        ts = data["graph"]["images"]["timeSeries"]
        pv = ts.get("preparedValues") or {}
        series = pv.get("absolute")
        if not series:  # ключа нет или пусто -> нет истории по фразе
            return "empty", []
        rows = [(date(int(p["year"]), int(p["month"]) + 1, 1), int(p["y"]))
                for p in series if 0 <= int(p["month"]) <= 11]
        return ("ok", rows) if rows else ("empty", [])
    except Exception as e:  # noqa: BLE001
        return "error", f"структура ответа: {str(e)[:80]}"


def _sanitize_phrase(s: str) -> str:
    """Wordstat getGraph давится на части пунктуации в фразе (/, :, ;, \\ и т.п.) —
    для повторной попытки заменяем их пробелами: «1000 л/мин» → «1000 л мин»
    (Wordstat считает их одной фразой, поэтому данные те же)."""
    return re.sub(r"\s+", " ", re.sub(r"[/:;\\]+", " ", s or "")).strip()


def _save(db, phrase, region, device, rows) -> int:
    from app.db.models import WordstatHistory
    from app.utils import query_hash
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:  # pragma: no cover
        raise RuntimeError(f"Unsupported dialect {dialect!r}")
    dev = "all" if device == "desktop,phone,tablet" else device
    qh = query_hash(phrase)
    vals = [{"query": phrase, "query_hash": qh, "region": region, "device": dev,
             "date": d, "value": v} for d, v in rows]
    if not vals:
        return 0
    stmt = insert(WordstatHistory).values(vals)
    stmt = stmt.on_conflict_do_update(
        index_elements=["query_hash", "region", "device", "date"],
        set_={"value": stmt.excluded.value, "query": stmt.excluded.query})
    db.execute(stmt)
    db.commit()
    return len(vals)


def _read_phrases(path) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


def _db_phrases(site_id=None) -> list[str]:
    from app.db.base import SessionLocal, init_db
    from app.db.models import Query
    from sqlalchemy import select
    init_db()
    db = SessionLocal()
    try:
        stmt = select(Query.text).distinct()
        if site_id:
            stmt = stmt.where(Query.site_id == site_id)
        return [t for (t,) in db.execute(stmt).all() if t]
    finally:
        db.close()


def _list_phrases() -> list[str]:
    """Phrases from the list uploaded on the «Спрос» page (AppSetting keylist)."""
    from app.db.base import SessionLocal, init_db
    from app.services import demand
    init_db()
    db = SessionLocal()
    try:
        return demand.get_keylist(db)
    finally:
        db.close()


def _find_sitekey(page) -> str | None:
    """Best-effort: read the Yandex SmartCaptcha sitekey from the page DOM."""
    try:
        return page.evaluate(
            """() => {
              const el = document.querySelector('[data-sitekey]');
              if (el) return el.getAttribute('data-sitekey');
              const m = document.documentElement.innerHTML.match(
                /sitekey["']?\\s*[:=]\\s*["']([A-Za-z0-9_\\-]+)["']/);
              return m ? m[1] : null;
            }""")
    except Exception:  # noqa: BLE001
        return None


def _inject_token(page, token: str) -> bool:
    """Put the solved token into the SmartCaptcha field and submit its form."""
    try:
        return bool(page.evaluate(
            """(token) => {
              let done = false;
              document.querySelectorAll('input[name="smart-token"]').forEach(i => {
                i.value = token;
                i.dispatchEvent(new Event('input', {bubbles: true}));
                i.dispatchEvent(new Event('change', {bubbles: true}));
                done = true;
                if (i.form) { try { i.form.submit(); } catch (e) {} }
              });
              return done;
            }""", token))
    except Exception:  # noqa: BLE001
        return False


def _auto_solve(page) -> bool:
    """Try to clear the captcha via the configured cloud solver. Returns True on
    apparent success (token injected). Never raises — caller falls back to manual."""
    from app.services import captcha as C
    provider, key = C.config()
    if not key:
        print("  ⚠ авто-решатель не настроен (нет ключа) — решаю вручную.", flush=True)
        return False
    try:
        page.bring_to_front()
        page.goto(WORDSTAT_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
    except Exception:  # noqa: BLE001
        pass
    sitekey = _find_sitekey(page)
    if not sitekey:
        print("  ⚠ не нашёл sitekey капчи на странице — решаю вручную.", flush=True)
        return False
    print(f"  🤖 решаю капчу через {provider} (sitekey {sitekey[:12]}…)…", flush=True)
    try:
        token = C.solve_smartcaptcha(sitekey, page.url, provider=provider, api_key=key)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ решатель не справился: {e}", flush=True)
        return False
    if _inject_token(page, token):
        page.wait_for_timeout(2500)
        print("  ✅ токен капчи внедрён.", flush=True)
        return True
    print("  ⚠ не удалось внедрить токен в страницу — решаю вручную.", flush=True)
    return False


def _solve_captcha(page, mode: str = "manual") -> None:
    """Clear a Yandex SmartCaptcha. ``mode``:
      manual  — show the window so you solve it by hand (default);
      auto    — try the cloud solver first, fall back to manual prompt;
      auto-only — cloud solver only (for unattended runs; no prompt)."""
    if mode in ("auto", "auto-only"):
        if _auto_solve(page):
            return
        if mode == "auto-only":
            print("  ⚠ авто-решение не удалось; жду 30с и пробую дальше.", flush=True)
            page.wait_for_timeout(30000)
            return
    try:
        page.bring_to_front()
        page.goto(WORDSTAT_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception:  # noqa: BLE001
        pass
    print("  ⚠ Капча Яндекса. Реши её в окне браузера, затем нажми Enter…", flush=True)
    try:
        input()
    except EOFError:
        page.wait_for_timeout(60000)


def cmd_collect(phrases, region, device, d_from, d_to, delay, limit,
                captcha="manual", skip_done=False) -> None:
    import random

    from app.db.base import SessionLocal, init_db
    from app.utils import query_hash
    from urllib.parse import quote

    if limit:
        phrases = phrases[:limit]
    if not phrases:
        sys.exit("Нет фраз для сбора (укажи --phrases файл, --from-list или --from-db).")
    init_db()
    db = SessionLocal()
    d_from = d_from or _default_range()[0]
    d_to = d_to or _default_range()[1]
    dev = "all" if device == "desktop,phone,tablet" else device

    if skip_done:  # resume: drop phrases already collected for this region/device/period
        done = _done_hashes(db, region, dev, _month_floor(d_from), _month_floor(d_to))
        before = len(phrases)
        phrases = [ph for ph in phrases if query_hash(ph) not in done]
        print(f"Возобновление: уже собрано {before - len(phrases)}, осталось {len(phrases)}.",
              flush=True)
        if not phrases:
            print("Всё уже собрано за этот период — нечего догружать.\n")
            db.close()
            cmd_status()
            return

    print(f"Сбор Wordstat: {len(phrases)} фраз, регион={region}, устройство={dev}, "
          f"период {d_from}–{d_to}, пауза ~{delay}с/запрос.", flush=True)
    ok = empty = fail = 0
    failed: list[str] = []
    err_dumps = 0
    os.makedirs(DEBUG_DIR, exist_ok=True)
    failed_path = os.path.join(DATA_DIR, "failed.txt")
    t0 = time.monotonic()
    with _pw()() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=False, viewport=VIEWPORT)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(WORDSTAT_URL, wait_until="domcontentloaded", timeout=60000)

        def _fetch(ph):
            """One phrase → (kind, data, last_txt, status). Retries captcha + transient errors."""
            ref = f"https://wordstat.yandex.ru/?region={region}&view=graph&words={quote(ph)}"
            k, d, txt, status = "error", None, "", 0
            for attempt in range(3):
                resp = ctx.request.post(
                    GRAPH_URL, data=json.dumps(_payload(ph, region, device, d_from, d_to)),
                    headers={"content-type": "application/json", "referer": ref}, timeout=45000)
                status = resp.status
                try:
                    txt = resp.text()
                except Exception:  # noqa: BLE001
                    txt = ""
                k, d = _parse_graph(resp)
                if k == "captcha":
                    _solve_captcha(page, captcha)
                    continue
                if k == "error" and attempt < 2:
                    page.wait_for_timeout(1500)  # transient — short pause, retry
                    continue
                break
            return k, d, txt, status

        for i, phrase in enumerate(phrases, 1):
            kind, data, last_txt, status = _fetch(phrase)
            used = phrase
            if kind == "error":  # fallback: Wordstat давится на пунктуации (/, : …)
                clean = _sanitize_phrase(phrase)
                if clean and clean != phrase:
                    k2, d2, t2, s2 = _fetch(clean)
                    if k2 in ("ok", "empty"):
                        kind, data, last_txt, status, used = k2, d2, t2, s2, clean
            elapsed = time.monotonic() - t0
            eta = (elapsed / i) * (len(phrases) - i)
            if kind == "ok":
                ok += 1
                n = _save(db, phrase, region, device, data)  # под ИСХОДНОЙ фразой
                note = f"{n} точек" + (" (очищено)" if used != phrase else "")
            elif kind == "empty":
                empty += 1
                note = "нет данных (низкочастотный)"
            else:  # error
                fail += 1
                failed.append(phrase)
                note = f"error: {data}"
                if err_dumps < 10 and last_txt:  # save raw response to diagnose the parser
                    with open(os.path.join(DEBUG_DIR, f"err_{err_dumps}.json"), "w",
                              encoding="utf-8") as f:
                        f.write(f"// phrase: {phrase}\n// status: {status}\n" + last_txt[:20000])
                    err_dumps += 1
            tail = f"ok={ok} empty={empty} fail={fail} ~{eta/60:.0f}м осталось"
            print(f"  [{i}/{len(phrases)}] «{phrase}» — {note} · {tail}", flush=True)
            # jitter the pause a bit so the request cadence isn't perfectly regular
            page.wait_for_timeout(int(delay * random.uniform(0.6, 1.5) * 1000))
        ctx.close()
    db.close()
    if failed:  # write the failed phrases so you can re-run just them
        with open(failed_path, "w", encoding="utf-8") as f:
            f.write("\n".join(failed) + "\n")
        print(f"\nОшибочные фразы ({len(failed)}) сохранены в {failed_path} — "
              f"повтор: --collect --phrases \"{failed_path}\"")
        if err_dumps:
            print(f"Сырые ответы по первым {err_dumps} ошибкам — в {DEBUG_DIR}\\err_*.json")
    print(f"\nГотово: собрано {ok}, без данных {empty}, ошибок {fail}.\n")
    cmd_status()  # show what's now in the DB so you can verify the run


def cmd_status() -> None:
    """Show what's already in wordstat_history (so you can verify a run)."""
    from app.db.base import SessionLocal, init_db
    from app.db.models import WordstatHistory
    from sqlalchemy import distinct, func, select
    init_db()
    db = SessionLocal()
    try:
        total = db.execute(select(func.count()).select_from(WordstatHistory)).scalar() or 0
        nq = db.execute(select(func.count(distinct(WordstatHistory.query_hash)))).scalar() or 0
        lo, hi = db.execute(select(func.min(WordstatHistory.date),
                                   func.max(WordstatHistory.date))).one()
        print(f"Wordstat в базе: фраз {nq}, строк {total}, период {lo}…{hi}")
        rows = db.execute(
            select(WordstatHistory.query, func.count(), func.max(WordstatHistory.value))
            .group_by(WordstatHistory.query)
            .order_by(func.max(WordstatHistory.value).desc()).limit(25)
        ).all()
        if rows:
            print("Топ фраз (по макс. частотности):")
            for q, c, mx in rows:
                print(f"  {mx:>11}  {c:>3} точек  {q}")
    finally:
        db.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="войти в Яндекс (headed)")
    ap.add_argument("--status", action="store_true", help="показать, что собрано в базе")
    ap.add_argument("--discover", metavar="QUERY", help="поймать эндпоинт «Истории» для фразы")
    ap.add_argument("--collect", action="store_true", help="собрать историю по списку фраз")
    ap.add_argument("--phrases", help="файл со списком фраз (по одной в строке)")
    ap.add_argument("--from-db", dest="from_db", action="store_true", help="фразы из таблицы Query")
    ap.add_argument("--from-list", dest="from_list", action="store_true",
                    help="фразы из списка, загруженного на вкладке «Спрос»")
    ap.add_argument("--site", type=int, help="с --from-db: только запросы этого site_id")
    ap.add_argument("--region", default="all", help="регион Wordstat (по умолч. all)")
    ap.add_argument("--device", default="desktop,phone,tablet", help="устройства")
    ap.add_argument("--from", dest="d_from", help="начало периода дд.мм.гггг")
    ap.add_argument("--to", dest="d_to", help="конец периода дд.мм.гггг")
    ap.add_argument("--delay", type=float, default=2.0, help="пауза между запросами, сек")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--captcha", choices=("manual", "auto", "auto-only"), default="manual",
                    help="как решать капчу: вручную (по умолч.) / авто-решатель / только авто")
    ap.add_argument("--skip-done", dest="skip_done", action="store_true",
                    help="пропустить фразы, уже собранные за этот регион/устройство/период "
                         "(для возобновления большого прогона)")
    ap.add_argument("--set-captcha", metavar="KEY",
                    help="сохранить ключ облачного решателя капч (шифруется)")
    ap.add_argument("--captcha-provider", choices=("capmonster", "anticaptcha", "2captcha"),
                    help="провайдер решателя капч (с --set-captcha; по умолч. capmonster)")
    a = ap.parse_args()
    if a.set_captcha or a.captcha_provider:
        from app.db.base import init_db
        from app.services import captcha as C
        init_db()
        C.save_config(provider=a.captcha_provider, api_key=a.set_captcha)
        prov, key = C.config()
        print(f"Решатель капч: провайдер={prov}, ключ {'задан' if key else 'НЕ задан'}.")
        return
    if a.login:
        cmd_login()
    elif a.status:
        cmd_status()
    elif a.discover:
        cmd_discover(a.discover)
    elif a.collect:
        if a.phrases:
            phrases = _read_phrases(a.phrases)
        elif a.from_list:
            phrases = _list_phrases()
        elif a.from_db:
            phrases = _db_phrases(a.site)
        else:
            phrases = []
        cmd_collect(phrases, a.region, a.device, a.d_from, a.d_to, a.delay, a.limit,
                    a.captcha, a.skip_done)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
