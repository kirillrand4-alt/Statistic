"""Wordstat history scraper — STEP 1: log in once and discover the internal
"История запросов" endpoint (the JSON the page loads for the dynamics graph).

Reuses the same logged-in Yandex profile as the Webvisor scraper, so login is
shared. Run --discover, open «История» for any phrase in the window, and this
captures the XHR/fetch JSON responses for inspection — then we build the actual
fetcher (per-phrase history) + captcha hook + storage on top.

  python scripts/wordstat.py --login                 # log into Yandex (headed)
  python scripts/wordstat.py --discover "компрессор"  # capture the history XHR

NOTE: scraping Wordstat is against Yandex ToS and may hit Yandex SmartCaptcha —
keep request rates low; this tool is for your own keyword research.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="войти в Яндекс (headed)")
    ap.add_argument("--discover", metavar="QUERY", help="поймать эндпоинт «Истории» для фразы")
    a = ap.parse_args()
    if a.login:
        cmd_login()
    elif a.discover:
        cmd_discover(a.discover)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
