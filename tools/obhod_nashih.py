"""Обход наших карточек: есть ли на сайте цена там, где в отчёте стоит «нет цены».

В отчёте «нет цены» означает «в прайс-выгрузке цены нет». Это НЕ то же самое, что
«цены нет на сайте»: из 850 проверенных позиций 94 цену на карточке показывают, а из
1 117 осушителей — 1 097. Поэтому перед тем как отдавать заказчику список «без цены»,
список надо прогнать здесь.

    python tools/obhod_nashih.py <файл.xlsx> <лист> <номер колонки с именем> <результат.csv> [папка с выгрузками]

Каждую строку пишем в csv СРАЗУ, а не печатаем срез в конце: прошлый прогон отдал
только первые 40 находок из 94, и полный список пришлось бы собирать заново.
"""
import csv
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
csv.field_size_limit(10 ** 9)

import openpyxl
from vendor_scrape import get
from price_ours import our_price

XLSX, SHEET, NCOL, OUT = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
S = Path(sys.argv[5] if len(sys.argv) > 5 else "data/ours")

ws = openpyxl.load_workbook(XLSX, data_only=True)[SHEET]
rows = [r for r in ws.iter_rows(min_row=2, values_only=True) if r[NCOL]]


def key(s):
    """Имя без категорийных слов — по нему карточка находится и в компакте, и в прайсе."""
    s = re.sub(r"(?i)\b(винтовой|дизельный|компрессор|спиральный|поршневой|адсорбционный|"
               r"рефрижераторный|осушитель|ресивер|передвижной)\b", " ", str(s))
    return re.sub(r"[^a-zа-я0-9]", "", s.lower())


def find(pat):
    """Свежий файл по маске: в папке лежат и бэкапы (.bak/.prev), брать их нельзя."""
    f = [p for p in S.rglob(pat) if not re.search(r"\.(bak|prev|old|copy)\b", p.name, re.I)]
    if not f:
        sys.exit(f"нет файла по маске {pat} в {S}")
    return max(f, key=lambda p: p.stat().st_mtime)


cat = {}
for r in csv.DictReader(open(find("specs_compact*.csv"), encoding="utf-8-sig"), delimiter=";"):
    n = (r.get("IE_NAME") or "").strip()
    if n:
        cat.setdefault(key(n), (r.get("IE_CODE") or "").strip())

price = {}
for rec in csv.reader(open(find("products_export*.csv"), encoding="utf-8-sig"), delimiter=";"):
    if len(rec) >= 3:
        try:
            price[key(rec[0])] = float(rec[2])
        except ValueError:
            pass

seen = {}
for r in rows:
    seen.setdefault(r[NCOL], r[0])
items = sorted(seen.items())
res = Counter()

with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.writer(fh, delimiter=";")
    w.writerow(["бренд", "товар", "цена на сайте", "цена в прайсе", "вердикт", "адрес"])
    for i, (name, brand) in enumerate(items, 1):
        code = cat.get(key(name))
        if not code:
            res["нет в каталоге"] += 1
            w.writerow([brand, name, "", "", "нет в каталоге", ""])
            continue
        u = f"https://prokompressor.ru/catalog/{code}/"
        try:
            p = get(u, timeout=30, tries=1)
        except Exception:
            res["ошибка загрузки"] += 1
            w.writerow([brand, name, "", "", "ошибка загрузки", u])
            continue
        v, why = our_price(p)
        res[why] += 1
        pr = price.get(key(name))
        w.writerow([brand, name, v or "", int(pr) if pr else "", why, u])
        fh.flush()
        if i % 150 == 0:
            print(f"... {i}/{len(items)}", flush=True)

print("ИТОГ:", dict(res), flush=True)
print("ФАЙЛ:", OUT, flush=True)
