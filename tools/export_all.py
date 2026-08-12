"""Выгрузки для выкладывания: по одному файлу на конкурента + наш полный каталог.

Зачем. Прогоны парсера складываются в общие `prices_<дата>_<время>.csv`, где
вперемешку все шесть площадок, а поздний прогон дополняет ранний. Чтобы отдать
данные наружу, их надо собрать по сайтам и схлопнуть дубли: одна строка на URL,
значения берутся из САМОГО ПОЗДНЕГО прогона, где поле непустое. Иначе свежая
цена из вечернего прогона теряется под пустым полем из утреннего.

Наш каталог отдаётся одним файлом: свойства из Битрикса плюс цена из фида —
ровно то, что видит матчер.

    python tools/export_all.py                 # в ./exports
    python tools/export_all.py --out /tmp/exp
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scrape_files
from scrape_files import find_ours

csv.field_size_limit(10**9)
SITES = ["compressortyt.ru", "aerocompressors.ru", "pnevmoteh.ru",
         "pnevmo-sklad.ru", "v-p-k.ru", "rutector.ru"]


def dm(url: str) -> str:
    u = (url or "").split("//")[-1].split("/")[0].lower()
    return u[4:] if u.startswith("www.") else u


def competitors(outdir: Path) -> list[tuple[str, int]]:
    """Схлопываем прогоны по URL: поздний файл переписывает непустые поля."""
    by_site: dict[str, dict[str, dict]] = defaultdict(dict)
    cols: dict[str, list[str]] = {}
    for path in scrape_files.SCRAPE_FILES:            # уже отсортированы по времени
        try:
            fh = open(path, encoding="utf-8-sig", errors="replace", newline="")
        except FileNotFoundError:
            continue
        with fh:
            rd = csv.DictReader(fh)
            for r in rd:
                u = (r.get("product_url") or "").strip()
                if not u:
                    continue
                s = dm(u)
                if s not in SITES:
                    continue
                cols.setdefault(s, list(r))
                cur = by_site[s].setdefault(u, {})
                for k, v in r.items():
                    if v not in (None, ""):
                        cur[k] = v
                # «Цена по запросу» в свежем прогоне гасит цену из старых: до фиксов
                # парсера карточки без своей цены получали цену чужого товара из блока
                # похожих, а правило «поздний непустой переписывает» само её не вымоет —
                # перескрейп отдаёт price пустым (см. brand_spec_review.load_comp_all).
                if (r.get("price_on_request") or "").strip() == "1":
                    for k in ("price", "old_price", "discount_pct"):
                        cur.pop(k, None)
    out = []
    for s in SITES:
        rows = by_site.get(s, {})
        dst = outdir / f"{s.replace('.', '_')}_{date.today():%Y%m%d}.csv"
        fields = cols.get(s) or ["product_url"]
        with open(dst, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for u in sorted(rows):
                w.writerow(rows[u])
        out.append((dst.name, len(rows)))
    return out


def ours(outdir: Path) -> tuple[str, int]:
    """Наш каталог: строка = товар, все свойства Битрикса + цена из фида."""
    specs = find_ours("specs_compact")
    prices = find_ours("products_export")
    rows: dict[str, dict] = {}
    with open(specs, encoding="utf-8-sig", errors="replace", newline="") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            code = (r.get("IE_CODE") or "").strip()
            if not code:
                continue
            cur = rows.setdefault(code, {})
            # Выгрузка Битрикса даёт по НЕСКОЛЬКО строк на товар (по строке на
            # значение свойства), поэтому собираем первое непустое по каждой колонке.
            for k, v in r.items():
                if v and not cur.get(k):
                    cur[k] = v.strip()
    price: dict[str, tuple[str, str]] = {}
    if prices and Path(prices).exists():
        for row in csv.reader(open(prices, encoding="utf-8-sig", errors="replace"), delimiter=";"):
            if len(row) >= 3 and row[1]:
                price[row[1].rstrip("/").split("/")[-1].lower()] = (row[1], row[2])
    fields = sorted({k for r in rows.values() for k in r})
    dst = outdir / f"nash_katalog_{date.today():%Y%m%d}.csv"
    with open(dst, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["URL", "ЦЕНА"] + fields,
                           delimiter=";", extrasaction="ignore")
        w.writeheader()
        for code, r in sorted(rows.items()):
            u, p = price.get(code.lower(), (f"https://prokompressor.ru/catalog/{code}/", ""))
            w.writerow({"URL": u, "ЦЕНА": p, **r})
    return dst.name, len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="exports")
    a = ap.parse_args()
    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)

    print(scrape_files.describe())
    print("\nконкуренты:")
    total = 0
    for name, n in competitors(outdir):
        print(f"  {name:40}{n:>9,}")
        total += n
    print(f"  {'ИТОГО':40}{total:>9,}")
    name, n = ours(outdir)
    print(f"\nнаш каталог:\n  {name:40}{n:>9,}")
    print(f"\n-> {outdir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
