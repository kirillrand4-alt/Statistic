# -*- coding: utf-8 -*-
"""Сверка НЕ парсером: заново качаем страницу и ищем сохранённое число как есть.

Смысл — проверить данные независимо от кода, который их добыл. Поэтому здесь НЕ
используется find_prices/main_price: берём цену из файла, нормализуем страницу
(убираем теги, разэкранируем, схлопываем пробелы) и просто смотрим, встречается ли
это число на странице в любом виде — «594372», «594 372», «594.372».
"""
import csv, glob, html, os, random, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"

def fetch(u):
    for _ in range(2):
        r = subprocess.run(["curl","-sS","-m","40","-L","--compressed","-A",UA,u],
                           capture_output=True, text=True, errors="replace")
        if r.stdout and len(r.stdout) > 800:
            return r.stdout
    return ""

def flat(page):
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S|re.I)
    t = html.unescape(html.unescape(re.sub(r"<[^>]+>", " ", t)))
    return re.sub(r"[\s\xa0 ]+", " ", t)

def check(row):
    page = fetch(row["url"])
    if not page:
        return row, "СТРАНИЦА НЕ ОТКРЫЛАСЬ"
    t = flat(page)
    v = row["price"]
    # число в любом виде: слитно, с пробелами, с точками между разрядами
    grp = re.sub(r"(?<=\d)(?=(\d{3})+$)", "|", v).split("|")
    pats = [v, r"[  ]?".join(grp), r"[.,]".join(grp)]
    ok = any(re.search(p, t) for p in pats)
    if not ok:
        # Цена варианта на видимой странице не показана — там только вариант по
        # умолчанию. У Berg остальные лежат в JSON предложений, у Sollant приходят по
        # ?oid=. Ищем и в сыром разэкранированном HTML: это те же данные сайта, просто
        # не отрисованные. Проверено адресно: Berg ВК-18.5Р 353 244 и Sollant SLT-55F
        # 1 321 588 — обе цены реальные, среди предложений своих карточек.
        raw = html.unescape(html.unescape(page))
        ok = any(re.search(p, raw) for p in pats)
    return row, ("совпало" if ok else f"НЕ НАЙДЕНО ({v})")

random.seed(int(sys.argv[1]) if len(sys.argv) > 1 else 21)
print(f"{'сайт':<14}{'проверено':>10}{'совпало':>9}{'не найдено':>12}{'не открылось':>14}")
bad_all = []
for f in sorted(glob.glob("vendor_data/*.csv")):
    if "_sverka" in f: continue
    rows = [r for r in csv.DictReader(open(f, encoding="utf-8-sig"), delimiter=";")
            if r.get("price") and r.get("url")]
    if not rows: continue
    smp = random.sample(rows, min(8, len(rows)))
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(check, smp))
    ok = sum(1 for _, s in res if s == "совпало")
    no = [(r, s) for r, s in res if s.startswith("НЕ НАЙДЕНО")]
    dead = sum(1 for _, s in res if s.startswith("СТРАНИЦА"))
    print(f"{os.path.basename(f)[:-4]:<14}{len(smp):>10}{ok:>9}{len(no):>12}{dead:>14}")
    bad_all += [(os.path.basename(f)[:-4], r, s) for r, s in no]
if bad_all:
    print("\nрасхождения:")
    for site, r, s in bad_all[:20]:
        print(f"  [{site}] {r['name'][:44]:<44} в файле {r['price']:>10} | {s}")
        print(f"      {r['url'][:100]}")
