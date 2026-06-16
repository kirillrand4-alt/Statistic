"""One-shot health check for schedulers & data collection (read-only).

Prints in one place:
  - APScheduler config (in-app daily GSC/Yandex collect),
  - per enabled site: last successful collection (date / rows) + last error,
  - Metrika freshness: latest visit date & count per counter,
  - systemd status of `seostat` and `seostat-metrika.timer` (best-effort).

Run:  .venv/bin/python scripts/check_health.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.base import SessionLocal, init_db  # noqa: E402
from app.db.models import CollectionRun, Site, Visit  # noqa: E402


def _sysd(*args) -> str:
    try:
        return subprocess.run(["systemctl", *args], capture_output=True,
                              text=True, timeout=8).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"(systemctl недоступен: {e})"


def main() -> None:
    s = get_settings()
    today = date.today()
    warn = []

    print("=== ПРОВЕРКА ПЛАНИРОВЩИКОВ И СБОРА ДАННЫХ ===\n")
    print(f"APScheduler (в приложении): {'ВКЛ' if s.enable_scheduler else 'ВЫКЛ ⚠'} "
          f"— ежедневный сбор GSC/Яндекс в {s.collect_cron_hour}:00, "
          f"переобновление последних {s.collect_refetch_days} дн.")
    if not s.enable_scheduler:
        warn.append("ENABLE_SCHEDULER=false — внутренний сбор GSC/Яндекс не запускается.")

    init_db()
    db = SessionLocal()
    try:
        sites = db.execute(select(Site).where(Site.enabled.is_(True)).order_by(Site.id)).scalars().all()
        print(f"\n--- GSC/Яндекс по сайтам (CollectionRun), включённых сайтов: {len(sites)} ---")
        for site in sites:
            label = site.display_name or site.property_uri or f"site {site.id}"
            ok = db.execute(
                select(CollectionRun.target_date, CollectionRun.rows_written)
                .where(CollectionRun.site_id == site.id, CollectionRun.status == "ok",
                       CollectionRun.target_date.isnot(None))
                .order_by(CollectionRun.target_date.desc()).limit(1)
            ).first()
            err = db.execute(
                select(CollectionRun.target_date, CollectionRun.error_text)
                .where(CollectionRun.site_id == site.id, CollectionRun.status == "error")
                .order_by(CollectionRun.id.desc()).limit(1)
            ).first()
            if ok and ok[0]:
                age = (today - ok[0]).days
                tag = " ⚠ давно!" if age > 3 else ""
                print(f"  [{site.id}] {label}: последний сбор до {ok[0]} ({age} дн. назад), строк {ok[1] or 0}{tag}")
                if age > 3:
                    warn.append(f"{label}: сбор GSC/Яндекс не обновлялся {age} дн.")
            else:
                print(f"  [{site.id}] {label}: успешных сборов нет ⚠")
                warn.append(f"{label}: нет успешных сборов GSC/Яндекс.")
            if err and err[1]:
                print(f"        ⚠ последняя ошибка ({err[0]}): {str(err[1])[:160]}")

        print("\n--- Метрика (визиты) по счётчикам ---")
        rows = db.execute(
            select(Visit.counter_id, func.max(Visit.date), func.count())
            .where(Visit.counter_id.isnot(None)).group_by(Visit.counter_id)
            .order_by(func.max(Visit.date).desc())
        ).all()
        if not rows:
            print("  визитов в базе нет ⚠")
            warn.append("Метрика: визитов в базе нет.")
        for cid, mx, n in rows:
            age = (today - mx).days if mx else None
            tag = " ⚠ устарело!" if (age is not None and age > 2) else ""
            print(f"  счётчик {cid}: последняя дата {mx} ({age} дн. назад), визитов {n:,}{tag}".replace(",", " "))
            if age is not None and age > 2:
                warn.append(f"Метрика счётчик {cid}: визиты не обновлялись {age} дн.")
    finally:
        db.close()

    print("\n--- systemd ---")
    print(f"  seostat (веб-приложение):        {_sysd('is-active', 'seostat')}")
    print(f"  seostat-metrika.timer (Метрика): {_sysd('is-active', 'seostat-metrika.timer')}")
    nxt = _sysd("list-timers", "seostat-metrika.timer", "--no-pager")
    for line in (nxt.splitlines()[:2] if nxt else []):
        print("   ", line)

    print("\n=== ИТОГ ===")
    if warn:
        print("Требует внимания:")
        for w in warn:
            print("  ⚠", w)
    else:
        print("✅ Всё свежо: планировщики собирают данные, проблем не видно.")


if __name__ == "__main__":
    main()
