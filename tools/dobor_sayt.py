"""Сверка каталога сайта с выгрузкой Битрикса и догрузка недостающих карточек.

РЕЗУЛЬТАТ СВЕРКИ 18.08: догружать нечего, выгрузка полная. В smart-sitemap 27 502
адреса, из них 27 376 карточек товара и 125 разделов; ВСЕ карточки есть в компакте
(27 434 кода — в нём ещё 68 позиций, которых нет в sitemap: скрытые и неактивные,
это норма). Обратное впечатление возникло из-за двух ошибок сравнения, обе мои:
коды в sitemap URL-кодированы («…200l_230%d0%b2»), а в выгрузке записаны кириллицей,
и без раскодирования 171 карточка выглядела пропавшей; разделы каталога («osushiteli»,
«po-tipu/vintovye») тоже считались товарами.

ВАЖНО, какой именно sitemap брать. У сайта их два, и обычный /sitemap.xml СЛОМАН:
lastmod 2025-11-27, а все адреса внутри схлопнуты в один «https://prokompressor.ru/
catalog/». Агент 18.08 проверял по нему отсутствие товара и получил ложный вывод
«286 карточек Ingersoll Rand выпали из sitemap» — в smart-sitemap все 286 на месте
(проверено: grep 'catalog/ir_' по всем 22 частям). Годится только smart-sitemap
(SMART ниже): 27 546 адресов каталога, lastmod свежий.

Отдельно снят вопрос, откуда взялось «серии нет в выгрузке» в отчётах агентов: им
показывалась подсказка nashi_toy_zhe_serii — а это карточки с СОВПАВШИМ ключом
серия+номер, обрезанные до 12 штук. Когда ключ у нас и у конкурента разъезжается
(«K-MAX 1110» против «K-MAX 11-10»), верная карточка в подсказку не попадает, хотя
в выгрузке она есть. Проверять наличие надо по коду в компакте, а не по подсказке.

Инструмент оставлен как регулярная сверка: сайт живёт своей жизнью, и как только
карточка появится на сайте раньше, чем в выгрузке, он её снимет и отдаст в том же
формате.

Что делает. Берёт smart-sitemap, вычитает коды, уже имеющиеся в компакте, и снимает
недостающие карточки со страниц: свойства размечены микроформатом
`itemprop="additionalProperty"`, поэтому парсится ровно то же, что видит покупатель.
Результат кладётся ОТДЕЛЬНЫМ файлом с теми же колонками IP_PROP — load_ours_all
читает каталог целиком, так что догруз подхватывается без правок матчера.

Почему отдельным файлом, а не дописыванием в компакт: компакт пересобирается из
выгрузки Битрикса (tools/bitrix_compact.py) и дописанное затёрлось бы при первом же
обновлении. Отдельный файл переживает пересборку и виден в diff.

    python tools/dobor_sayt.py -o data/ours/specs_dobor.csv
    python tools/dobor_sayt.py --limit 20 -o /tmp/proba.csv     # прогон на пробу
"""
from __future__ import annotations

import argparse
import csv
import html
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scrape_files import find_ours

csv.field_size_limit(10**9)

SMART = "https://prokompressor.ru/smart_sitemap/sitemap-smart.xml"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Название свойства на странице -> колонка компакта. Соответствие взято из
# PROPS_BITRIX.md, чтобы догруз читался теми же правилами, что и выгрузка.
FIELDS = {
    "производитель":        "IP_PROP22553",
    "мощность":             "IP_PROP22562",
    "производительность":   "IP_PROP22571",
    "рабочее давление":     "IP_PROP22573",
    "объем ресивера":       "IP_PROP22564",
    "ресивер":              "IP_PROP22574",
    "осушитель":            "IP_PROP22565",
    "частотный преобразователь": "IP_PROP22586",
    "тип смазки":           "IP_PROP22583",
    "тип привода":          "IP_PROP22601",
    "вес":                  "IP_PROP22555",
    "тип охлаждения":       "IP_PROP22669",
    "степень защиты двигателя": "IP_PROP22959",
    "ip электродвигателя":  "IP_PROP22674",
    "производитель двигателя": "IP_PROP22569",
    "модель двигателя":     "IP_PROP23013",
    "габариты":             "IP_PROP22556",
}
OUT_COLS = ["IE_CODE", "IE_NAME", "IE_ID", "IE_ACTIVE"] + sorted(set(FIELDS.values()))

_PROP = re.compile(r'itemprop="additionalProperty".*?itemprop="name"[^>]*>(.*?)</[a-z]+>'
                   r'.*?itemprop="value"[^>]*>(.*?)</(?:span|div)>', re.S | re.I)


def fetch(url: str, tries: int = 3) -> str:
    for i in range(tries):
        r = subprocess.run(["curl", "-sS", "-m", "60", "-A", UA, url],
                           capture_output=True, text=True)
        if r.stdout and len(r.stdout) > 2000:
            return r.stdout
        time.sleep(2 * (i + 1))
    return ""


def clean(x: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x))).strip(" —- ")


def smart_urls() -> list[str]:
    idx = fetch(SMART)
    maps = re.findall(r"<loc>\s*(\S+?)\s*</loc>", idx)
    out = set()
    for m in maps:
        body = fetch(m)
        out |= {u for u in re.findall(r"<loc>\s*(https://prokompressor\.ru/[^\s<]+)\s*</loc>", body)
                if "/catalog/" in u}
    return sorted(out)


def parse(url: str, page: str) -> dict | None:
    """Свойства карточки -> строка компакта. None, если страница не отдала характеристик."""
    name = re.search(r"<title>(.*?)</title>", page, re.S)
    name = clean(name.group(1)).split(" - цена")[0] if name else ""
    row = {c: "" for c in OUT_COLS}
    row["IE_CODE"] = unquote(url.rstrip("/").split("/")[-1])
    row["IE_NAME"] = name
    row["IE_ACTIVE"] = "Y"
    sku = re.search(r'"sku"\s*:\s*"(\d+)"', page)
    if sku: row["IE_ID"] = sku.group(1)
    got = 0
    for k, v in _PROP.findall(page):
        k, v = clean(k).lower(), clean(v)
        if not v: continue
        # «Мощность, кВт» -> «мощность»: единица уже зашита в колонку компакта
        key = k.split(",")[0].strip()
        col = FIELDS.get(key)
        if col and not row[col]:
            row[col] = v
            got += 1
    return row if got >= 3 else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default="specs_dobor.csv")
    ap.add_argument("--limit", type=int, default=0, help="снять не больше N карточек")
    a = ap.parse_args()

    have = set()
    src = find_ours("specs_compact")
    if src:
        with open(src, encoding="utf-8-sig", errors="replace") as fh:
            for r in csv.DictReader(fh, delimiter=";"):
                c = (r.get("IE_CODE") or "").strip().lower()
                if c: have.add(c)
    print(f"в компакте кодов: {len(have):,}")

    urls = smart_urls()
    print(f"в smart-sitemap адресов каталога: {len(urls):,}")
    # раздел каталога и страницы фильтров товарами не являются: у них короткий код
    # без цифр («bezmaslyanye», «10-bar», «do-15-kvt»)
    todo = [u for u in urls
            if unquote(u.rstrip("/").split("/")[-1]).lower() not in have
            and len(unquote(u.rstrip("/").split("/")[-1])) > 14
            and any(ch.isdigit() for ch in u.rstrip("/").split("/")[-1])]
    if a.limit: todo = todo[:a.limit]
    print(f"к догрузке: {len(todo):,}\n")

    rows, skip = [], 0
    for i, u in enumerate(todo, 1):
        r = parse(u, fetch(u))
        if r: rows.append(r)
        else: skip += 1
        if i % 25 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)} | снято {len(rows)} | без характеристик {skip}", flush=True)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_COLS, delimiter=";")
        w.writeheader()
        for r in rows: w.writerow(r)
    print(f"\nзаписано {len(rows)} карточек -> {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
