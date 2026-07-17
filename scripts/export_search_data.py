"""Выгрузка поисковых данных по ВСЕМ хостам отдельными CSV-файлами в папку.

По правилам снятия: единый период для всех, каждый хост отдельной строкой (в т.ч.
городские поддомены), нулевые строки НЕ выкидываются, Google и Яндекс раздельно
(колонки «Источник»/«Сайт»). Пишет по файлу на уровень данных:
  search_query_<период>.csv   — запрос × страница (у GSC полный кросс; у Яндекса
                                 запрос + его основная страница)
  search_page_<период>.csv    — постранично
  search_device_<период>.csv  — страница × устройство (desktop/mobile)
  search_totals_<период>.csv  — суточные тоталы по хосту

    python scripts/export_search_data.py --out C:\seostat\drop\drop-storage
    python scripts/export_search_data.py --out C:\seostat\drop\drop-storage --days 30
    python scripts/export_search_data.py --out ... --start 2026-06-18 --end 2026-07-15
    python scripts/export_search_data.py --out ... --levels query,device --per-host
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.base import SessionLocal, init_db  # noqa: E402
from app.providers.base import DateRange  # noqa: E402
from app.services.bulk_export import LEVELS, bulk_dataframe  # noqa: E402
from app.services.export import _prettify  # noqa: E402


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", str(name)).strip("_") or "host"


def _write_csv(df, path: str) -> int:
    # utf-8-sig + ; — корректно открывается в Excel с кириллицей
    df.to_csv(path, sep=";", index=False, encoding="utf-8-sig")
    return len(df)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help=r"папка назначения, напр. C:\seostat\drop\drop-storage")
    ap.add_argument("--days", type=int, default=30, help="сколько последних дней (по умолч. 30)")
    ap.add_argument("--start", help="начало периода ГГГГ-ММ-ДД (перекрывает --days)")
    ap.add_argument("--end", help="конец периода ГГГГ-ММ-ДД (по умолч. вчера)")
    ap.add_argument("--levels", default="query,page,device,totals",
                    help="уровни через запятую: query,page,device,totals")
    ap.add_argument("--per-host", dest="per_host", action="store_true",
                    help="отдельный файл на каждый хост (иначе один файл на уровень со всеми хостами)")
    a = ap.parse_args()

    end = datetime.date.fromisoformat(a.end) if a.end else datetime.date.today() - datetime.timedelta(days=1)
    start = datetime.date.fromisoformat(a.start) if a.start else end - datetime.timedelta(days=a.days - 1)
    dr = DateRange(start=start, end=end)
    levels = [x.strip() for x in a.levels.split(",") if x.strip() in LEVELS]
    if not levels:
        sys.exit(f"Нет валидных уровней. Доступны: {', '.join(LEVELS)}")
    os.makedirs(a.out, exist_ok=True)
    tag = f"{start.isoformat()}_{end.isoformat()}"

    init_db()
    db = SessionLocal()
    try:
        print(f"Период {start}…{end}, уровни: {', '.join(levels)}\nПапка: {a.out}\n")
        for level in levels:
            df = _prettify(bulk_dataframe(db, dr, level))
            if df is None or df.empty:
                print(f"  {level:7} — данных нет, пропуск")
                continue
            if a.per_host and "Сайт" in df.columns:
                for host, sub in df.groupby("Сайт"):
                    path = os.path.join(a.out, f"search_{level}_{_safe(host)}_{tag}.csv")
                    print(f"  {level:7} · {host} → {_write_csv(sub, path)} строк")
            else:
                path = os.path.join(a.out, f"search_{level}_{tag}.csv")
                print(f"  {level:7} → {_write_csv(df, path)} строк  ({os.path.basename(path)})")
        print("\nГотово.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
