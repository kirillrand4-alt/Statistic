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
    graph_reqs: list[str] = []  # bodies of getGraph POSTs (most reliable — request side)

    def on_req(req):
        try:
            if "/wordstat/api/getGraph" in req.url and req.method == "POST":
                graph_reqs.append(req.post_data or "")
        except Exception:  # noqa: BLE001
            pass

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
        page.on("request", on_req)
        page.on("response", on_resp)
        page.goto(WORDSTAT_URL, wait_until="domcontentloaded", timeout=60000)
        print(f"Открыт Wordstat. Введи «{query}», «Динамика», переключай «По месяцам/неделям/дням».")
        print("Жду 90 сек (или нажми Enter раньше)…")
        try:
            page.wait_for_timeout(2000)
            input()
        except EOFError:
            page.wait_for_timeout(90000)
        if "passport" in page.url or "auth" in page.url:
            print("  ⚠ не залогинен — сначала: --login")
        ctx.close()

    # getGraph request bodies — the exact payload the UI sends per granularity
    for i, body in enumerate(graph_reqs):
        with open(os.path.join(DEBUG_DIR, f"getgraph_req_{i}.txt"), "w", encoding="utf-8") as f:
            f.write(body)
    print(f"\ngetGraph-запросов поймано: {len(graph_reqs)} (сохранены в getgraph_req_*.txt)")
    for i, body in enumerate(graph_reqs):
        print(f"\n[getGraph {i}] {body[:500]}")

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


def _done_hashes(db, region, dev, lo, hi, graph="month", match="broad") -> set:
    """query_hash set already having ANY data for region/device/granularity/match within
    the window — these phrases are fully collected (saved atomically), so a resumed run
    can skip them. Monthly uses wordstat_history; day/week use wordstat_series."""
    from sqlalchemy import and_, distinct, select

    if graph == "month":
        from app.db.models import WordstatHistory as W
        cond = and_(W.region == region, W.device == dev, W.match_type == match,
                    W.date >= lo, W.date <= hi)
    else:
        from app.db.models import WordstatSeries as W
        cond = and_(W.region == region, W.device == dev, W.granularity == graph,
                    W.match_type == match, W.date >= lo, W.date <= hi)
    rows = db.execute(select(distinct(W.query_hash)).where(cond)).all()
    return {h for (h,) in rows}


def _align_week(dfrom: str, dto: str) -> tuple[str, str]:
    """Snap a dd.mm.YYYY range to whole weeks — Wordstat's weekly graph requires the
    start on a Monday and the end on a Sunday (иначе HTTP 400)."""
    from datetime import datetime, timedelta
    df = datetime.strptime(dfrom, "%d.%m.%Y").date()
    dt_ = datetime.strptime(dto, "%d.%m.%Y").date()
    df -= timedelta(days=df.weekday())               # -> понедельник (Mon=0)
    dt_ -= timedelta(days=(dt_.weekday() + 1) % 7)    # -> воскресенье
    return df.strftime("%d.%m.%Y"), dt_.strftime("%d.%m.%Y")


def _default_range(graph="month") -> tuple[str, str]:
    """Default window per granularity (dd.mm.YYYY): month/week — 24 мес (макс Wordstat;
    неделя выровнена пн…вс), day — последние ~60 дней (дневные данные ограничены)."""
    from datetime import date, timedelta
    today = date.today()
    if graph == "day":
        end = today - timedelta(days=1)
        return (end - timedelta(days=59)).strftime("%d.%m.%Y"), end.strftime("%d.%m.%Y")
    end = today.replace(day=1) - timedelta(days=1)  # last day of previous month
    sy, sm = end.year, end.month - 23               # 24-month window
    while sm <= 0:
        sm += 12
        sy -= 1
    lo, hi = date(sy, sm, 1).strftime("%d.%m.%Y"), end.strftime("%d.%m.%Y")
    return _align_week(lo, hi) if graph == "week" else (lo, hi)


def _payload(phrase, region, device, d_from, d_to, graph="month") -> dict:
    return {
        "currentDevice": device, "currentGraphType": graph, "dbname": "rus",
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
        # Two shapes: month points = {year, month(0-based), y}; day/week points =
        # {day: "YYYY-MM-DD", y} (month/year null). Skip points with null value.
        rows = []
        for p in series:
            v = p.get("y", p.get("value"))
            if v is None:
                continue
            day = p.get("day") or p.get("x")               # day: "day"; week: "x"
            if isinstance(day, str) and "-" in day:        # ISO date string (day/week start)
                try:
                    d = date.fromisoformat(day[:10])
                except ValueError:
                    continue
            else:                                          # month: year + 0-based month
                try:
                    yr, mo = int(p["year"]), int(p["month"])
                except (TypeError, ValueError, KeyError):
                    continue
                if not (0 <= mo <= 11):
                    continue
                d = date(yr, mo + 1, int(day) if day else 1)
            rows.append((d, int(v)))
        return ("ok", rows) if rows else ("empty", [])
    except Exception as e:  # noqa: BLE001
        return "error", f"структура ответа: {str(e)[:80]}"


def _match_query(phrase: str, match: str = "broad") -> str:
    """Wrap a phrase in the Wordstat operator for a frequency match type:
    broad — как есть; phrase — «"фраза"»; exact — «"!фраза"» (точные словоформы);
    order — «"[!фраза]"» (+ фиксированный порядок слов)."""
    if match == "broad":
        return phrase
    words = phrase.split()
    if match == "phrase":
        return f'"{phrase}"'
    if match == "exact":
        return '"' + " ".join("!" + w for w in words) + '"'
    if match == "order":
        return '"[' + " ".join("!" + w for w in words) + ']"'
    return phrase


def _phrase_key(s: str) -> str:
    """Word-order/regcase/ё-insensitive key. Wordstat без операторов игнорирует
    порядок слов, поэтому «винтовой компрессор» = «компрессор винтовой» = один и тот
    же запрос. Используется, чтобы не запрашивать явные дубли повторно.
    (Морфологию не сводим — «компрессоры» ≠ «компрессор» — это безопаснее.)"""
    s = re.sub(r"[^\w\s]", " ", (s or "").lower().replace("ё", "е"))
    return " ".join(sorted(w for w in s.split() if w))


def _value_map(db, region, dev, match="broad") -> dict:
    """phrase -> tuple of its monthly values (same region/device/match). Phrases with an
    identical tuple are the same Wordstat query (used as the dedup equivalence)."""
    from sqlalchemy import select

    from app.db.models import WordstatHistory as W
    m: dict = {}
    for q, _d, v in db.execute(
            select(W.query, W.date, W.value)
            .where(W.region == region, W.device == dev, W.match_type == match)
            .order_by(W.query, W.date)).all():
        m.setdefault(q, []).append(int(v or 0))
    return {q: tuple(vals) for q, vals in m.items()}


def _series_rows(db, region, dev, granularity, match="broad") -> dict:
    """phrase -> [(date, value), …] already collected for a day/week granularity/match."""
    from sqlalchemy import select

    from app.db.models import WordstatSeries as WS
    out: dict = {}
    for q, d, v in db.execute(
            select(WS.query, WS.date, WS.value)
            .where(WS.region == region, WS.device == dev, WS.granularity == granularity,
                   WS.match_type == match)
            .order_by(WS.query, WS.date)).all():
        out.setdefault(q, []).append((d, int(v or 0)))
    return out


def _eq_key(phrase, value_map):
    """Equivalence key: identical monthly series → same query (('v', …)); phrases with
    no monthly reference fall back to word-order key (('w', …))."""
    vk = value_map.get(phrase)
    return ("v",) + vk if vk else ("w", _phrase_key(phrase))


def _sanitize_phrase(s: str) -> str:
    """Wordstat getGraph давится на части пунктуации в фразе (/, :, ;, \\ и т.п.) —
    для повторной попытки заменяем их пробелами: «1000 л/мин» → «1000 л мин»
    (Wordstat считает их одной фразой, поэтому данные те же)."""
    return re.sub(r"\s+", " ", re.sub(r"[/:;\\]+", " ", s or "")).strip()


def _save(db, phrase, region, device, rows, graph="month", match="broad") -> int:
    from app.db.models import WordstatHistory, WordstatSeries
    from app.utils import query_hash
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:  # pragma: no cover
        raise RuntimeError(f"Unsupported dialect {dialect!r}")
    dev = "all" if device == "desktop,phone,tablet" else device
    # match-aware hash -> different match types are distinct rows without touching the
    # unique constraint; the stored query stays clean for display
    qh = query_hash(_match_query(phrase, match))
    if not rows:
        return 0
    if graph == "month":  # monthly -> historic table
        vals = [{"query": phrase, "query_hash": qh, "region": region, "device": dev,
                 "match_type": match, "date": d, "value": v} for d, v in rows]
        stmt = insert(WordstatHistory).values(vals)
        stmt = stmt.on_conflict_do_update(
            index_elements=["query_hash", "region", "device", "date"],
            set_={"value": stmt.excluded.value, "query": stmt.excluded.query,
                  "match_type": stmt.excluded.match_type})
    else:  # day / week -> fine-grained table (granularity keeps them separate)
        vals = [{"query": phrase, "query_hash": qh, "region": region, "device": dev,
                 "granularity": graph, "match_type": match, "date": d, "value": v}
                for d, v in rows]
        stmt = insert(WordstatSeries).values(vals)
        stmt = stmt.on_conflict_do_update(
            index_elements=["query_hash", "region", "device", "granularity", "date"],
            set_={"value": stmt.excluded.value, "query": stmt.excluded.query,
                  "match_type": stmt.excluded.match_type})
    db.execute(stmt)
    db.commit()
    return len(vals)


def _read_phrases(path) -> list[str]:
    # utf-8-sig strips a BOM (PowerShell `Set-Content -Encoding UTF8` adds one)
    with open(path, encoding="utf-8-sig") as f:
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
                captcha="manual", skip_done=False, graph="month", dedup=False,
                match="broad") -> None:
    import random
    from datetime import datetime

    from sqlalchemy import select

    from app.db.base import SessionLocal, init_db
    from app.utils import query_hash
    from urllib.parse import quote

    if limit:
        phrases = phrases[:limit]
    if not phrases:
        sys.exit("Нет фраз для сбора (укажи --phrases файл, --from-list или --from-db).")
    init_db()
    db = SessionLocal()
    d_from = d_from or _default_range(graph)[0]
    d_to = d_to or _default_range(graph)[1]
    if graph == "week":  # Wordstat weekly needs Monday…Sunday, иначе 400
        d_from, d_to = _align_week(d_from, d_to)
    dev = "all" if device == "desktop,phone,tablet" else device

    if skip_done:  # resume: drop phrases already collected for this region/device/period
        if graph == "month":
            lo, hi = _month_floor(d_from), _month_floor(d_to)
        else:
            lo = datetime.strptime(d_from, "%d.%m.%Y").date()
            hi = datetime.strptime(d_to, "%d.%m.%Y").date()
        done = _done_hashes(db, region, dev, lo, hi, graph, match)
        before = len(phrases)
        phrases = [ph for ph in phrases if query_hash(_match_query(ph, match)) not in done]
        print(f"Возобновление: уже собрано {before - len(phrases)}, осталось {len(phrases)}.",
              flush=True)
        if not phrases:
            print("Всё уже собрано за этот период — нечего догружать.\n")
            db.close()
            cmd_status()
            return

    # dedup by VALUE-equivalence: phrases with an identical monthly series are the
    # same Wordstat query, so their day/week series match too — collect one, copy to
    # the rest (no request). Fallback for phrases without monthly data: word order.
    eq_rows: dict = {}   # equivalence key -> representative rows (for skip + replicate)
    value_key: dict = {}

    def eqkey(ph):
        return _eq_key(ph, value_key)

    if dedup:
        value_key = _value_map(db, region, dev, match)
        if graph != "month":  # pre-seed already-collected day/week data (resume + copy)
            for q, rows in _series_rows(db, region, dev, graph, match).items():
                eq_rows.setdefault(eqkey(q), rows)
        uniq = len({eqkey(ph) for ph in phrases})
        with_ref = sum(1 for ph in phrases if ph in value_key)
        print(f"Дедуп по значениям: фраз {len(phrases)} → уникальных запросов ~{uniq} "
              f"(дублей ~{len(phrases) - uniq} скопируем без запроса). "
              f"С месячным эталоном — {with_ref}, уже готовых групп — {len(eq_rows)}.", flush=True)

    print(f"Сбор Wordstat ({graph}/{match}): {len(phrases)} фраз, регион={region}, "
          f"устройство={dev}, период {d_from}–{d_to}, пауза ~{delay}с/запрос.", flush=True)
    ok = empty = fail = dup = 0
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
                    GRAPH_URL, data=json.dumps(_payload(ph, region, device, d_from, d_to, graph)),
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
            if dedup and eqkey(phrase) in eq_rows:  # same query already known — copy, no request
                dup += 1
                rep = eq_rows[eqkey(phrase)]
                n = _save(db, phrase, region, device, rep, graph, match) if rep else 0
                print(f"  [{i}/{len(phrases)}] «{phrase}» — дубль, скопировано {n} точек · "
                      f"ok={ok} empty={empty} dup={dup} fail={fail}", flush=True)
                continue
            kind, data, last_txt, status = _fetch(_match_query(phrase, match))
            used = phrase
            if kind == "error" and match == "broad":  # sanitize только для широкого (в операторах пунктуация нужна)
                clean = _sanitize_phrase(phrase)
                if clean and clean != phrase:
                    k2, d2, t2, s2 = _fetch(clean)
                    if k2 in ("ok", "empty"):
                        kind, data, last_txt, status, used = k2, d2, t2, s2, clean
            if i == 1 and graph != "month" and last_txt:  # verify the day/week shape
                with open(os.path.join(DEBUG_DIR, f"sample_{graph}.json"), "w",
                          encoding="utf-8") as f:
                    f.write(f"// phrase: {phrase}\n// graph: {graph}\n" + last_txt[:20000])
            elapsed = time.monotonic() - t0
            eta = (elapsed / i) * (len(phrases) - i)
            if kind == "ok":
                ok += 1
                n = _save(db, phrase, region, device, data, graph, match)  # под ИСХОДНОЙ фразой
                if dedup:
                    eq_rows[eqkey(phrase)] = data  # representative — copy to its dupes later
                note = f"{n} точек" + (" (очищено)" if used != phrase else "")
            elif kind == "empty":
                empty += 1
                if dedup:
                    eq_rows[eqkey(phrase)] = []  # no data → dupes are empty too
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
            tail = f"ok={ok} empty={empty} dup={dup} fail={fail} ~{eta/60:.0f}м осталось"
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
    print(f"\nГотово: собрано {ok}, без данных {empty}, дублей скопировано {dup}, ошибок {fail}.\n")
    cmd_status()  # show what's now in the DB so you can verify the run


def cmd_probe(phrase, region, device, graph) -> None:
    """Find the max window Wordstat accepts for a granularity — try increasing windows
    on one phrase until HTTP 400. Prints the largest window that worked (in days)."""
    from datetime import date, timedelta
    from urllib.parse import quote

    cands = ((30, 45, 60, 62, 65, 70, 75, 80, 85, 90, 120, 180) if graph == "day"
             else (90, 180, 270, 365, 540, 730, 900, 1095, 1460))  # week/month — дни
    print(f"Проба макс. окна ({graph}) на «{phrase}» (регион={region})…", flush=True)
    best = 0
    with _pw()() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=False, viewport=VIEWPORT)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(WORDSTAT_URL, wait_until="domcontentloaded", timeout=60000)
        ref = f"https://wordstat.yandex.ru/?region={region}&view=graph&words={quote(phrase)}"
        for n in cands:
            end = date.today() - timedelta(days=1)
            start = end - timedelta(days=n - 1)
            sfrom, sto = start.strftime("%d.%m.%Y"), end.strftime("%d.%m.%Y")
            if graph == "week":
                sfrom, sto = _align_week(sfrom, sto)
            resp = ctx.request.post(
                GRAPH_URL, data=json.dumps(_payload(phrase, region, device, sfrom, sto, graph)),
                headers={"content-type": "application/json", "referer": ref}, timeout=45000)
            kind, data = _parse_graph(resp)
            npts = len(data) if isinstance(data, list) else 0
            print(f"  окно {n:>4} дн (~{n // 30} мес) → {kind} (HTTP {resp.status}, точек {npts})",
                  flush=True)
            if kind in ("ok", "empty"):
                best = n
            elif resp.status == 400:
                break
            elif kind == "captcha":
                _solve_captcha(page, "manual")
                continue
            page.wait_for_timeout(1500)
        ctx.close()
    print(f"\nМаксимальное окно ({graph}): ~{best} дней (~{best // 30} мес; дальше — HTTP 400).")


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
        # fine-grained (day/week) series, if any
        from app.db.models import WordstatSeries as WS
        for g in ("day", "week"):
            n = db.execute(select(func.count(distinct(WS.query_hash)))
                           .where(WS.granularity == g)).scalar() or 0
            if n:
                t = db.execute(select(func.count()).where(WS.granularity == g)).scalar() or 0
                glo, ghi = db.execute(select(func.min(WS.date), func.max(WS.date))
                                      .where(WS.granularity == g)).one()
                label = "по дням" if g == "day" else "по неделям"
                print(f"Wordstat {label}: фраз {n}, строк {t}, период {glo}…{ghi}")
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
    ap.add_argument("--graph", choices=("month", "week", "day", "all"), default="month",
                    help="гранулярность: month (по умолч.) / week / day / all (все три)")
    ap.add_argument("--match", choices=("broad", "phrase", "exact", "order", "all"),
                    default="broad",
                    help="тип частотности: broad — как есть; phrase — «\"фраза\"»; "
                         "exact — «\"!фраза\"»; order — «\"[!фраза]\"»; all — все 4")
    ap.add_argument("--dedup", action="store_true",
                    help="не запрашивать дубли: фразы с одинаковым месячным рядом — один "
                         "запрос Wordstat, остальным копируем данные (экономит запросы/капчу)")
    ap.add_argument("--probe", action="store_true",
                    help="найти макс. окно Wordstat для гранулярности из --graph "
                         "(перебор окон на одной фразе)")
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
    elif a.probe:
        src = (_read_phrases(a.phrases) if a.phrases else
               (_list_phrases() if a.from_list else _db_phrases(a.site) if a.from_db else []))
        cmd_probe(src[0] if src else "компрессор", a.region, a.device, a.graph)
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
        grans = ("month", "week", "day") if a.graph == "all" else (a.graph,)
        matches = ("broad", "phrase", "exact", "order") if a.match == "all" else (a.match,)
        # month first (its broad/… эталон помогает дедупу недели/дня)
        for g in grans:
            for mt in matches:
                if len(grans) * len(matches) > 1:
                    print(f"\n===== {g} / {mt} =====", flush=True)
                cmd_collect(phrases, a.region, a.device, a.d_from, a.d_to, a.delay, a.limit,
                            a.captcha, a.skip_done, g, a.dedup, mt)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
