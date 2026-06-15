"""Webvisor session recording — sizing (смета) now; recorder is the next step.

Webvisor replays have no API, so recording = browser automation of the Metrica
UI (Playwright/Chromium + screen capture). This CLI currently answers HOW MANY
sessions and WHICH (visit_id) to record for a period, from our synced visits —
run it first to size the job (Метрика хранит Вебвизор ≈15 дней).

    python scripts/webvisor.py --domain example.com --from 2026-06-01 --count
    python scripts/webvisor.py --domain example.com --from 2026-06-01 --list

The actual recorder needs, on the server: Playwright+Chromium+ffmpeg installed,
a saved Yandex login session, and a short recon of the live Webvisor UI (the
replay URL + player controls). Until that's wired, --record explains the setup.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.base import SessionLocal, init_db  # noqa: E402
from app.providers.base import DateRange  # noqa: E402
from app.services import webvisor as W  # noqa: E402

_SETUP = """\
Рекордер ещё не настроен. Для записи Вебвизора на сервере нужно:
  1) зависимости:  .venv/bin/pip install playwright && .venv/bin/playwright install --with-deps chromium
                   apt install -y ffmpeg
  2) вход в Яндекс: один раз залогиниться и сохранить сессию (storage_state)
  3) рекон живого Вебвизора: URL открытого реплея сессии + кнопки плеера
Пришли мне (2)–(3), и я допишу запись. Пока — оцени объём: --count / --list."""


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
    ap.add_argument("--no-bots", dest="no_bots", action="store_true",
                    help="исключить роботов (ym:s:isRobot; нужна пере-синхронизация визитов)")
    ap.add_argument("--count", action="store_true", help="сколько сессий за период")
    ap.add_argument("--list", action="store_true", help="вывести visit_id сессий")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--record", action="store_true", help="(скоро) записать видео сессий")
    a = ap.parse_args()

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
        kw = {"source": a.source, "min_page_views": a.min_pv,
              "min_duration": a.min_dur, "exclude_bots": a.no_bots}

        if a.record:
            print(_SETUP)
            return
        if a.no_bots and not W.has_robot_flag(db, sid):
            print("⚠ Флаг робота ещё не синхронизирован для этого сайта — --no-bots ничего не отсечёт.")
            print("  Пере-синкни визиты за период, потом повтори (см. scripts/metrika_logs.py).")
        n = W.count_sessions(db, sid, dr, **kw)
        print(f"Сессий к записи: {n} (сайт {sid}, {d1}…{d2}"
              + (f", источник {a.source}" if a.source else "")
              + (f", ≥{a.min_pv} стр." if a.min_pv else "")
              + (f", >{a.min_dur}с" if a.min_dur else "")
              + (", без ботов" if a.no_bots else "") + ")")
        if a.list:
            for s in W.sessions_for_period(db, sid, dr, limit=(a.limit or None), **kw):
                print(f"  {s['date']}  visit={s['visit_id']}  стр={s['page_views']}  "
                      f"{s['duration']}с  [{s['source']}]  {s['start_url'] or ''}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
