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
import math
import os
import sys
import time
from datetime import date, timedelta

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
# Deep link to a single session replay. UNVERIFIED — confirm via --probe/recon and
# override with --replay-url or env WEBVISOR_REPLAY_URL. {counter} and {visit_id}
# are substituted.
REPLAY_URL = os.environ.get("WEBVISOR_REPLAY_URL", "")
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


def _replay_url(counter, visit_id, tmpl) -> str:
    return (tmpl or WEBVISOR_BASE).format(counter=counter or "", visit_id=visit_id or "")


def cmd_probe(sessions, tmpl) -> None:
    os.makedirs(DEBUG_DIR, exist_ok=True)
    s = sessions[0] if sessions else {"counter_id": "", "visit_id": ""}
    url = _replay_url(s.get("counter_id"), s.get("visit_id"), tmpl)
    print(f"Probe: открываю {url}")
    with _pw()() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=True, viewport=VIEWPORT)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)
        shot, html = os.path.join(DEBUG_DIR, "probe.png"), os.path.join(DEBUG_DIR, "probe.html")
        page.screenshot(path=shot, full_page=False)
        with open(html, "w", encoding="utf-8") as f:
            f.write(page.content())
        print(f"  итоговый URL: {page.url}\n  скриншот: {shot}\n  html: {html}")
        if "passport" in page.url or "auth" in page.url:
            print("  ⚠ похоже, не залогинен — сначала пройди --login.")
        ctx.close()


def _done_ids() -> set[str]:
    if not os.path.isdir(OUT_DIR):
        return set()
    return {f[:-5] for f in os.listdir(OUT_DIR) if f.endswith(".webm")}


def cmd_record(sessions, tmpl, speed, buffer_s, limit) -> None:
    if not (tmpl or REPLAY_URL):
        sys.exit("Не задан URL реплея. После рекона запусти с --replay-url "
                 "'https://metrika.yandex.ru/...{counter}...{visit_id}...'")
    os.makedirs(OUT_DIR, exist_ok=True)
    done = _done_ids()
    todo = [s for s in sessions if s["visit_id"] not in done]
    if limit:
        todo = todo[:limit]
    rec_secs = sum(math.ceil((s["duration"] or 0) / speed) + buffer_s for s in todo)
    print(f"К записи: {len(todo)} (уже есть {len(done)}). Скорость x{speed}. "
          f"Ориентир: ~{rec_secs // 60} мин записи, ~{rec_secs * 0.3 / 1024:.1f} ГБ (грубо).")
    man = open(os.path.join(DATA_DIR, "manifest.csv"), "a", encoding="utf-8")
    ok = fail = 0
    with _pw()() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=True, viewport=VIEWPORT,
            record_video_dir=OUT_DIR, record_video_size=VIEWPORT)
        for i, s in enumerate(todo, 1):
            vid = s["visit_id"]
            url = _replay_url(s.get("counter_id"), vid, tmpl)
            secs = math.ceil((s["duration"] or 0) / speed) + buffer_s
            page = ctx.new_page()
            video = page.video
            try:
                page.goto(url, wait_until="networkidle", timeout=60000)
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
            if i % 25 == 0 or i == len(todo):
                print(f"  [{i}/{len(todo)}] ok={ok} fail={fail}", flush=True)
        ctx.close()
    man.close()
    print(f"Готово: записано {ok}, ошибок {fail}. Видео в {OUT_DIR}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", type=int, help="site_id (иначе резолв по --domain)")
    ap.add_argument("--domain", help="домен (берём сайт с наибольшим числом визитов)")
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
    ap.add_argument("--record", action="store_true", help="записать видео сессий")
    ap.add_argument("--replay-url", dest="replay", default="", help="шаблон URL реплея ({counter},{visit_id})")
    ap.add_argument("--speed", type=float, default=1.0, help="множитель скорости плеера (бюджет времени)")
    ap.add_argument("--buffer", type=int, default=4, help="доп. секунд на сессию (загрузка/буфер)")
    a = ap.parse_args()

    if a.login:  # no DB needed to log in
        cmd_login()
        return

    init_db()
    db = SessionLocal()
    try:
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
        kw = {"source": a.source, "min_page_views": a.min_pv, "min_duration": a.min_dur}
        sessions = W.sessions_for_period(db, sid, dr, limit=(a.limit or None), **kw)

        if a.probe:
            cmd_probe(sessions, a.replay)
            return
        if a.record:
            cmd_record(sessions, a.replay, a.speed, a.buffer, a.limit)
            return

        n = W.count_sessions(db, sid, dr, **kw)
        print(f"Сессий к записи: {n} (сайт {sid}, {d1}…{d2}"
              + (f", источник {a.source}" if a.source else "")
              + (f", ≥{a.min_pv} стр." if a.min_pv else "")
              + (f", >{a.min_dur}с" if a.min_dur else "") + ")")
        if a.list:
            for s in sessions:
                print(f"  {s['date']}  visit={s['visit_id']}  стр={s['page_views']}  "
                      f"{s['duration']}с  [{s['source']}]  {s['start_url'] or ''}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
