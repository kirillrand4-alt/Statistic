"""Webvisor: size, log in, probe and record session replays.

Webvisor replays have no API — they're only viewable in the Metrica web UI — so
recording = browser automation (Playwright/Chromium screencast to .webm). We pick
WHICH sessions to record (and how many) for a period from our synced ``visit``
table, then drive the player for each.

Modes:
  --count / --list      size the job (how many sessions, which visit_id)
  --login               open a real browser (via VNC) to log into Metrica once;
                        the session is saved in a persistent profile dir
  --probe               open the Webvisor page for the first matching session,
                        save a screenshot + HTML + URL (for tuning selectors/URL)
  --record              record video for every matching session (resumable)

Auth is a persistent Chromium profile (--profile), so --login is done once and
--record/--probe reuse it headless. Recording length is bounded by each visit's
known duration (duration/speed + buffer), so no fragile "playback ended" probe.

Metrica keeps Webvisor recordings ≈15 days, so older visits can't be replayed.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import sys
import time
from datetime import date, timedelta
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.base import SessionLocal, init_db  # noqa: E402
from app.providers.base import DateRange  # noqa: E402
from app.services import webvisor as W  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("WEBVISOR_DATA", os.path.join(REPO, "data", "webvisor"))
PROFILE_DIR = os.environ.get("WEBVISOR_PROFILE", os.path.join(DATA_DIR, "profile"))
OUT_DIR = os.environ.get("WEBVISOR_OUT", os.path.join(DATA_DIR, "videos"))
DEBUG_DIR = os.path.join(DATA_DIR, "debug")

# The Webvisor list page for a counter (used by --login / --probe fallback).
WEBVISOR_BASE = os.environ.get("WEBVISOR_BASE", "https://metrika.yandex.ru/webvisor/{counter}")
# Deep link to a single session replay (discovered via recon). {visit_id}, {date}
# and {counter} come from our DB. user_id_hash is intentionally omitted — the page
# resolves the session from visit_id; override with --replay-url / env if needed.
REPLAY_URL = os.environ.get(
    "WEBVISOR_REPLAY_URL",
    "https://metrika.yandex.ru/inpage/visor-proto?id={counter}&offset=0&date={date}"
    "&date_visit=&visit_id={visit_id}&watch_id=&user_id_hash={user_id_hash}&dn=&tld=ru",
)
PLAY_SELECTOR = os.environ.get("WEBVISOR_PLAY_SELECTOR", "")  # optional; many players autoplay
VIEWPORT = {"width": 1366, "height": 768}


def _pw():
    """Lazy import so --count/--list work without Playwright installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright не установлен. Поставь: .venv/bin/pip install playwright "
                 "&& .venv/bin/playwright install --with-deps chromium")
    return sync_playwright


def cmd_login() -> None:
    os.makedirs(PROFILE_DIR, exist_ok=True)
    if not os.environ.get("DISPLAY"):
        print("Нет DISPLAY — запусти под xvfb, напр.:  xvfb-run -a -s '-screen 0 1366x768x24' \\\n"
              "  .venv/bin/python scripts/webvisor.py --login   (и подключись по VNC)")
    with _pw()() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=False, viewport=VIEWPORT)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://metrika.yandex.ru/", wait_until="load")
        print("Браузер открыт. В окне (через VNC) войди в Яндекс и открой Вебвизор нужного счётчика.")
        try:
            input("Когда залогинился — нажми Enter здесь, чтобы сохранить сессию и закрыть… ")
        except EOFError:
            time.sleep(180)
        ctx.close()
    print(f"Профиль сохранён в {PROFILE_DIR}. Проверь вход: --probe")


def _replay_url(counter, visit_id, vdate, user_id_hash, tmpl) -> str:
    t = tmpl or REPLAY_URL or WEBVISOR_BASE
    return t.format(counter=counter or "", visit_id=visit_id or "", date=vdate or "",
                    user_id_hash=user_id_hash or "")


def _load_hashes() -> dict:
    """visit_id -> user_id_hash, from sessions.jsonl produced by --harvest."""
    out, f = {}, os.path.join(DATA_DIR, "sessions.jsonl")
    if os.path.exists(f):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("visit_id") and d.get("user_id_hash"):
                out[str(d["visit_id"])] = str(d["user_id_hash"])
    return out


def cmd_probe(sessions, tmpl) -> None:
    os.makedirs(DEBUG_DIR, exist_ok=True)
    hashes = _load_hashes()
    s = (next((x for x in sessions if str(x.get("visit_id")) in hashes), None)
         or (sessions[0] if sessions else {"counter_id": "", "visit_id": "", "date": ""}))
    uh = hashes.get(str(s.get("visit_id")), "")
    if not uh:
        print("  ⚠ нет сессии с user_id_hash — сначала запусти --harvest (иначе откроется ошибка).")
    url = _replay_url(s.get("counter_id"), s.get("visit_id"), s.get("date"), uh, tmpl)
    print(f"Probe: открываю {url}")
    js = ("() => { const out=[]; "
          "for (const e of document.querySelectorAll("
          "'button,[role=button],[class*=speed],[class*=Speed],[class*=rate],[class*=Rate],"
          "[class*=playback],[class*=Playback],[class*=control],[class*=Control],[data-speed]')) "
          "{ out.push({tag:e.tagName, t:(e.innerText||'').trim().slice(0,24), "
          "title:e.getAttribute('title')||'', aria:e.getAttribute('aria-label')||'', "
          "cls:(''+(e.className||'')).slice(0,70)}); } return out.slice(0,80); }")
    with _pw()() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=True, viewport=VIEWPORT)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(6000)
        shot = os.path.join(DEBUG_DIR, "probe.png")
        page.screenshot(path=shot, full_page=False)
        with open(os.path.join(DEBUG_DIR, "probe.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
        print(f"  URL: {page.url}\n  скриншот: {shot}")
        if "passport" in page.url or "auth" in page.url:
            print("  ⚠ не залогинен — сначала пройди --login.")
        for fr in page.frames:
            try:
                ctrls = fr.evaluate(js)
            except Exception:
                continue
            if not ctrls:
                continue
            print(f"  -- frame {fr.url[:55]} : {len(ctrls)} контролов")
            for c in ctrls:
                blob = (c["t"] + c["title"] + c["aria"] + c["cls"]).lower()
                star = "  ⭐" if any(k in blob for k in
                                    ("speed", "rate", "playback", "скорост", "x2", "x4", "x8")) else "    "
                print(f"{star}<{c['tag']}> t='{c['t']}' title='{c['title']}' "
                      f"aria='{c['aria']}' cls='{c['cls']}'")
        try:  # role-based scan pierces shadow DOM, where player controls may live
            btns = page.get_by_role("button")
            n = btns.count()
            print(f"  role=button (сквозь shadow): {n}")
            for i in range(min(n, 70)):
                b = btns.nth(i)
                txt = ((b.text_content() or "").strip())[:24]
                aria = b.get_attribute("aria-label") or ""
                title = b.get_attribute("title") or ""
                if txt or aria or title:
                    blob = (txt + aria + title).lower()
                    star = "  ⭐" if any(k in blob for k in
                                        ("speed", "скорост", "x2", "x4", "x8", "×")) else "    "
                    print(f"{star}btn[{i}] '{txt}' aria='{aria}' title='{title}'")
        except Exception as e:
            print("  role-scan:", e)
        ctx.close()


def cmd_discover(counter) -> None:
    """Open the Webvisor list and capture the XHR that carries session data
    (visit_id + user_id_hash), so we learn the internal endpoint + its shape."""
    os.makedirs(DEBUG_DIR, exist_ok=True)
    url = f"https://metrika.yandex.ru/stat/visor?id={counter}&period=today"
    print(f"Discover: открываю список {url}")
    needles = ["3320642230537945170", "212544696", "3320726018304245762", "3663266546"]
    NAMES = ("getList", "webvisor-data-api")
    SKIP_CT = ("image", "font", "css", "javascript", "video", "octet", "html")
    grabbed, seen = [], []

    def on_resp(resp):
        try:
            ct = (resp.headers.get("content-type", "") or "").lower()
            if any(x in ct for x in SKIP_CT):
                return
            u = resp.url
            body = resp.text()
        except Exception:
            return
        seen.append(u)
        hit_needle = any(nd in body for nd in needles)
        if hit_needle or any(s in u for s in NAMES):
            try:
                method, post = resp.request.method, resp.request.post_data
            except Exception:
                method, post = "?", None
            grabbed.append((u, ct, method, post, body, hit_needle))

    with _pw()() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=True, viewport=VIEWPORT)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", on_resp)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(12000)
        try:
            page.mouse.wheel(0, 6000)  # nudge any lazy-loading list
            page.wait_for_timeout(5000)
        except Exception:
            pass
        if "passport" in page.url or "auth" in page.url:
            print("  ⚠ не залогинен — сначала пройди --login.")
        ctx.close()

    print(f"\nОтветов просмотрено: {len(seen)}; интересных: {len(grabbed)}")
    for i, (u, ct, method, post, b, hn) in enumerate(grabbed[:10]):
        path = os.path.join(DEBUG_DIR, f"hit_{i}.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(b)
        if post:
            with open(path + ".post.txt", "w", encoding="utf-8") as f:
                f.write(post)
        tag = "  <-- содержит наш visit_id!" if hn else ""
        print(f"\n[{i}] {method} {u}\n   ct={ct} len={len(b)}{tag}  ({path})")
        if post:
            print(f"   POST: {unquote(post)[:700]}")
        try:
            print(f"   shape: {_shape(json.loads(b))[:700]}")
        except Exception:
            pass
        found = False
        for nd in needles:
            j = b.find(nd)
            if j >= 0:
                found = True
                print(f"   рядом с {nd}: …{b[max(0, j - 140):j + 70]}…")
        if not found:
            print(f"   тело[:300]: {b[:300]}")
    if not grabbed:
        print("Не нашёл список сессий. Все просмотренные текстовые URL:")
        for u in seen[-25:]:
            print("  ", u)


def _shape(d, depth=0):
    if isinstance(d, dict):
        if depth >= 3:
            return "{…}"
        items = list(d.items())[:10]
        return "{" + ",".join(f"{k}:{_shape(v, depth + 1)}" for k, v in items) + ("…}" if len(d) > 10 else "}")
    if isinstance(d, list):
        return f"[{len(d)}×{_shape(d[0], depth + 1)}]" if d else "[]"
    return "str" if isinstance(d, str) else type(d).__name__


def cmd_inspect() -> None:
    """Inspect JSON bodies saved by --discover (no shell special chars): print each
    file's shape, hash/visit-ish field names, where our known visit_id/user_id_hash
    sit, and a snippet after the first "data" so we can read a real session row."""
    pat = re.compile(r'"([A-Za-z_]*(?:hash|visit|watch|uid|user|client|uniq|durat)[A-Za-z_]*)"', re.I)
    needles = ["3320642230537945170", "212544696", "3320726018304245762", "3663266546"]
    files = sorted(glob.glob(os.path.join(DEBUG_DIR, "api_*.json")) +
                   glob.glob(os.path.join(DEBUG_DIR, "hit_*.json")))
    if not files:
        print("Нет файлов api_*/hit_*.json — сначала запусти --discover.")
        return
    for f in files:
        t = open(f, encoding="utf-8").read()
        try:
            shape = _shape(json.loads(t))
        except Exception:
            shape = "<не JSON>"
        names = sorted(set(pat.findall(t)), key=str.lower)
        print(f"\n{os.path.basename(f)} (len={len(t)})\n  shape: {shape[:500]}\n  поля: {names}")
        for nd in needles:
            j = t.find(nd)
            if j >= 0:
                print(f"  нашёл {nd}: …{t[max(0, j - 90):j + 50]}…")
        k = t.find('"data"')
        if k >= 0 and len(t) > 3000:
            print(f"  после data: …{t[k:k + 240]}…")


# Exact dimensions the Webvisor SPA requests (order matters): visit_id is at
# index 0, user_id_hash at index 4 — we mirror it so the request is identical.
_HARVEST_DIMS = (
    "ym:s:visitID,ym:s:webVisorViewed,ym:s:webVisorSelected,ym:s:webVisorSelectedText,"
    "ym:s:userIDHash,ym:s:webVisorVersion,ym:s:trafficSource,ym:s:regionCountry,"
    "ym:s:operatingSystem,ym:s:browser,ym:s:dateTime,ym:s:webVisorActivity,"
    "ym:s:visitDurationShort,ym:s:pageViewsShort,ym:s:searchPhraseWithLink,"
    "ym:s:refererDomainShort,ym:s:userVisitsShort,ym:s:webVisorGoals"
)


def cmd_harvest(counters, d1, d2) -> None:
    """Page through getList for EACH counter (reusing the SPA CSRF key + cookies),
    one day at a time, collecting visit_id -> user_id_hash (+ search phrase) into
    data/webvisor/sessions.jsonl."""
    getlist = "https://metrika.yandex.ru/i-proxy/i-webvisor-data-api/getList?lang=ru"
    key = {"v": None}

    def on_req(req):
        if "getList" in req.url and not key["v"]:
            m = re.search(r"key=([^&]+)", req.post_data or "")
            if m:
                key["v"] = unquote(m.group(1))  # decode %3A back to ':' (don't double-encode)

    days, a, b = [], date.fromisoformat(d1), date.fromisoformat(d2)
    while a <= b:
        days.append(a.isoformat()); a += timedelta(days=1)
    sessions, seen = [], set()
    with _pw()() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=True, viewport=VIEWPORT)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("request", on_req)
        page.goto(f"https://metrika.yandex.ru/stat/visor?id={counters[0]}&period=today",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        if "passport" in page.url or "auth" in page.url:
            print("Не залогинен — сначала пройди --login."); ctx.close(); return
        if not key["v"]:
            print("Не удалось получить ключ API (список не загрузился)."); ctx.close(); return
        headers = {"x-requested-with": "XMLHttpRequest", "origin": "https://metrika.yandex.ru",
                   "referer": "https://metrika.yandex.ru/stat/visor"}
        print(f"Ключ получен. Счётчиков: {len(counters)}, дней: {len(days)}.")
        for counter in counters:
            csum = 0
            for day in days:
                offset = 1
                while offset < 200000:
                    args = json.dumps([{"offset": offset, "limit": 200, "date1": day, "date2": day,
                                        "sort": "-ym:s:dateTime", "id": str(counter),
                                        "dimensions": _HARVEST_DIMS}])
                    try:
                        r = ctx.request.post(getlist, form={"args": args, "key": key["v"], "lang": "ru"},
                                             headers=headers)
                        data = r.json()
                        err = data.get("error") or {}
                        if err.get("name") == "MetrikaSecretKeyError" and err.get("args"):
                            key["v"] = str(err["args"][0])  # server returns the expected key — retry
                            r = ctx.request.post(getlist, form={"args": args, "key": key["v"], "lang": "ru"},
                                                 headers=headers)
                            data = r.json()
                    except Exception as e:
                        print(f"  counter {counter} {day} off {offset}: ошибка {e}"); break
                    rows = (data.get("result") or {}).get("data") or []
                    for row in rows:
                        dn = row.get("dimensions") or []
                        vid = dn[0].get("name") if len(dn) > 0 else None
                        uh = dn[4].get("name") if len(dn) > 4 else None
                        if vid and uh and vid not in seen:
                            seen.add(vid)
                            phrase = dn[14].get("name") if len(dn) > 14 else None
                            sessions.append({"visit_id": vid, "user_id_hash": uh, "phrase": phrase})
                            csum += 1
                    if len(rows) < 200:
                        break
                    offset += 200
            print(f"  counter {counter}: +{csum} (итого {len(sessions)})", flush=True)
        ctx.close()

    os.makedirs(DATA_DIR, exist_ok=True)
    out = os.path.join(DATA_DIR, "sessions.jsonl")
    with open(out, "w", encoding="utf-8") as f:
        for s in sessions:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"\nСобрано сессий: {len(sessions)} (с user_id_hash) -> {out}")


def _clear_profile_lock() -> None:
    """Remove stale Chromium Singleton* locks left by an orphaned browser (e.g. after
    Ctrl+C) so launch_persistent_context doesn't hang waiting for the profile lock.
    Safe while no other webvisor browser command runs at the same time."""
    for f in glob.glob(os.path.join(PROFILE_DIR, "Singleton*")):
        try:
            os.remove(f)
        except OSError:
            pass


def _done_ids() -> set[str]:
    if not os.path.isdir(OUT_DIR):
        return set()
    return {f[:-5] for f in os.listdir(OUT_DIR) if f.endswith(".webm")}


def cmd_record(sessions, tmpl, speed, buffer_s, limit, max_sec=0) -> None:
    hashes = _load_hashes()
    if not hashes:
        sys.exit("Нет собранных user_id_hash — сначала запусти --harvest.")
    os.makedirs(OUT_DIR, exist_ok=True)
    done = _done_ids()
    with_hash = [s for s in sessions if str(s["visit_id"]) in hashes]
    todo = [s for s in with_hash if s["visit_id"] not in done]
    if limit:
        todo = todo[:limit]

    def _secs(dur):
        n = math.ceil((dur or 0) / speed) + buffer_s
        return min(n, max_sec) if max_sec else n

    rec_secs = sum(_secs(s["duration"]) for s in todo)
    print(f"К записи: {len(todo)} (уже есть {len(done)}; "
          f"без записи в Вебвизоре: {len(sessions) - len(with_hash)}). Скорость x{speed}"
          + (f", кап {max_sec}с" if max_sec else "")
          + f". Ориентир: ~{rec_secs // 60} мин, ~{rec_secs * 0.06 / 1024:.1f} ГБ (грубо).",
          flush=True)
    man = open(os.path.join(DATA_DIR, "manifest.csv"), "a", encoding="utf-8")
    ok = fail = 0
    _clear_profile_lock()
    with _pw()() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=True, viewport=VIEWPORT,
            record_video_dir=OUT_DIR, record_video_size=VIEWPORT)
        for pg in list(ctx.pages):  # drop the blank auto-opened page (and its stray video)
            v = pg.video
            try:
                pg.close()
                if v:
                    os.remove(v.path())
            except Exception:
                pass
        for i, s in enumerate(todo, 1):
            vid = s["visit_id"]
            url = _replay_url(s.get("counter_id"), vid, s.get("date"), hashes[str(vid)], tmpl)
            secs = _secs(s["duration"])
            page = ctx.new_page()
            video = page.video
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if PLAY_SELECTOR:
                    try:
                        page.click(PLAY_SELECTOR, timeout=5000)
                    except Exception:
                        pass
                page.wait_for_timeout(secs * 1000)
                page.close()
                src = video.path() if video else None
                if src and os.path.exists(src):
                    os.replace(src, os.path.join(OUT_DIR, f"{vid}.webm"))
                    ok += 1
                    man.write(f"{vid},ok,{secs}\n")
                else:
                    fail += 1
                    man.write(f"{vid},no-video,{secs}\n")
            except Exception as e:  # one bad session must not kill the run
                fail += 1
                man.write(f"{vid},error,{str(e)[:80]}\n")
                try:
                    page.close()
                except Exception:
                    pass
            man.flush()
            print(f"  [{i}/{len(todo)}] visit={vid} ok={ok} fail={fail}", flush=True)
        ctx.close()
    man.close()
    print(f"Готово: записано {ok}, ошибок {fail}. Видео в {OUT_DIR}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", type=int, help="site_id (иначе резолв по --domain)")
    ap.add_argument("--domain", help="домен (берём сайт с наибольшим числом визитов)")
    ap.add_argument("--all", dest="all_domains", action="store_true", help="все домены (все счётчики)")
    ap.add_argument("--oldest", action="store_true", help="начинать со старых сессий (по возрастанию даты)")
    ap.add_argument("--exclude", help="исключить домен(ы), чей адрес содержит подстроку (напр. berg)")
    ap.add_argument("--from", dest="d1", help="дата с (по умолч. 14 дней назад — ретенция Вебвизора)")
    ap.add_argument("--to", dest="d2", help="дата по (по умолч. сегодня)")
    ap.add_argument("--source", help="фильтр по источнику трафика (напр. organic/ad/direct)")
    ap.add_argument("--min-pageviews", dest="min_pv", type=int, default=0)
    ap.add_argument("--min-duration", dest="min_dur", type=int, default=0,
                    help="только сессии длиннее N секунд (например 10)")
    ap.add_argument("--count", action="store_true", help="сколько сессий за период")
    ap.add_argument("--list", action="store_true", help="вывести visit_id сессий")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--login", action="store_true", help="войти в Яндекс (headful, через VNC)")
    ap.add_argument("--probe", action="store_true", help="открыть 1 сессию: скриншот+html+URL (рекон)")
    ap.add_argument("--discover", action="store_true", help="найти внутренний API списка Вебвизора (перехват сети)")
    ap.add_argument("--inspect", action="store_true", help="разобрать сохранённые ответы --discover (имена полей)")
    ap.add_argument("--harvest", action="store_true", help="собрать visit_id+user_id_hash из getList (нужно перед --record)")
    ap.add_argument("--record", action="store_true", help="записать видео сессий")
    ap.add_argument("--replay-url", dest="replay", default="", help="шаблон URL реплея ({counter},{visit_id})")
    ap.add_argument("--speed", type=float, default=1.0, help="множитель скорости плеера (бюджет времени)")
    ap.add_argument("--buffer", type=int, default=4, help="доп. секунд на сессию (загрузка/буфер)")
    ap.add_argument("--max-seconds", dest="max_sec", type=int, default=0,
                    help="кап записи на сессию, сек (0 = без капа; бережёт место/время)")
    a = ap.parse_args()

    if a.login:  # no DB needed to log in
        cmd_login()
        return
    if a.inspect:  # no DB needed — just reads files saved by --discover
        cmd_inspect()
        return

    init_db()
    db = SessionLocal()
    try:
        if a.all_domains:
            sid = None
        else:
            sid = W.resolve_visit_site(db, site_id=a.site, domain=a.domain)
            if not sid:
                avail = W.sites_with_visits(db)
                if avail:
                    print("Не нашёл сайт по запросу. Доступные домены с визитами:")
                    for x in avail:
                        print(f"  --domain {x['domain']}   (site {x['site_id']}, визитов {x['visits']})")
                else:
                    print("В базе нет визитов Метрики — сначала закачай их (scripts/metrika_logs.py).")
                return
        d2 = date.fromisoformat(a.d2) if a.d2 else date.today()
        d1 = date.fromisoformat(a.d1) if a.d1 else d2 - timedelta(days=13)
        dr = DateRange(start=d1, end=d2)
        exclude = W.counters_matching(db, a.exclude) if a.exclude else None
        if a.exclude:
            print(f"Исключаю по '{a.exclude}' счётчики: {exclude or 'ничего не нашёл'}")
        kw = {"source": a.source, "min_page_views": a.min_pv, "min_duration": a.min_dur,
              "exclude_counters": exclude}
        sessions = W.sessions_for_period(db, sid, dr, oldest=a.oldest, limit=(a.limit or None), **kw)

        if a.discover:
            cs = W.distinct_counters(db, sid)
            if not cs:
                print("Нет счётчиков с визитами в базе.")
                return
            cmd_discover(cs[0])
            return
        if a.harvest:
            counters = [c for c in W.distinct_counters(db, sid) if not exclude or c not in exclude]
            if not counters:
                print("Нет счётчиков с визитами в базе.")
                return
            cmd_harvest(counters, d1.isoformat(), d2.isoformat())
            return
        if a.probe:
            cmd_probe(sessions, a.replay)
            return
        if a.record:
            cmd_record(sessions, a.replay, a.speed, a.buffer, a.limit, a.max_sec)
            return

        n = W.count_sessions(db, sid, dr, **kw)
        label = "все домены" if sid is None else f"сайт {sid}"
        print(f"Сессий к записи: {n} ({label}, {d1}…{d2}"
              + (f", источник {a.source}" if a.source else "")
              + (f", ≥{a.min_pv} стр." if a.min_pv else "")
              + (f", >{a.min_dur}с" if a.min_dur else "")
              + (", старые первыми" if a.oldest else "")
              + (f", без '{a.exclude}'" if a.exclude else "") + ")")
        if a.list:
            for s in sessions:
                print(f"  {s['date']}  visit={s['visit_id']}  стр={s['page_views']}  "
                      f"{s['duration']}с  [{s['source']}]  {s['start_url'] or ''}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
