"""Актуальные цены из фида -> в файл, который уже читает матчер.

Зачем. Матчер берёт специи и цены из РАЗНЫХ файлов нашего каталога:

    specs_compact.csv        свойства из Битрикса (IP_PROP*) — их и оставляем
    products_export_*.csv    цены, три колонки «название;URL;цена», без заголовка

Цены в выгрузке Битрикса отстают от сайта, а в фиде актуальные. Поэтому меняем
только второй файл: скрипт читает фид и пишет `products_export_feed_<дата>.csv`
в том же формате в OURS_DIR (имя с пометкой feed, чтобы не затереть выгрузку
Битрикса того же числа — она нужна как запасной источник цен). `scrape_files.find_ours("products_export")` берёт самый
свежий по времени изменения, так что новый файл подхватится сам — правок в
brand_spec_review.py не нужно.

Склейка с Битриксом идёт по СЛАГУ URL (последний сегмент), он же `IE_CODE`.

По умолчанию цены СЛИВАЮТСЯ с текущим файлом: из фида берётся то, что он знает,
остальное остаётся как было. Иначе товары вне фида молча теряют цену — а без цены
карточка выпадает из сравнения, ради которого всё и делается. Отключается
флагом --only-feed, если фид заведомо полнее выгрузки.

Вход — что угодно из трёх:

    CSV с именованными колонками  (`URL` + `Цена: Сайт (RUB)`, как в
                                   prokompressor.ru.csv; имена ищутся по смыслу)
    YML/XML фид, файл             (<offer><url>…</url><price>…</price></offer>)
    YML/XML фид, ссылка           (http/https — скачается)

    python tools/prices_from_feed.py feed.xml
    python tools/prices_from_feed.py prokompressor.ru.csv --dry-run
    python tools/prices_from_feed.py https://site/yml/ --out C:/parser/data/ours
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scrape_files import OURS_DIR, find_ours

csv.field_size_limit(10**9)

# Колонки фида ищем по смыслу, а не по точному имени: выгрузки называют их
# по-разному («Цена: Сайт (RUB)», «Price», «Цена»), а порядок не фиксирован.
_URL_COL = re.compile(r"^(url|ссылка|адрес|link)$", re.I)
_PRICE_COL = re.compile(r"цена|price", re.I)
_NAME_COL = re.compile(r"^(название|наименование|name|title|товар)$", re.I)
# «Цена: Старая», «Цена закупки» — не то, что показывает сайт. Берём ту, где
# явно сказано «сайт», иначе первую подходящую.
_PRICE_SITE = re.compile(r"сайт|site|розн", re.I)


def slug(url: str) -> str:
    return (url or "").strip().rstrip("/").split("/")[-1].lower()


def num(x) -> float | None:
    m = re.search(r"\d+(?:[.,]\d+)?", str(x).replace(" ", "").replace("\xa0", ""))
    if not m:
        return None
    v = float(m.group().replace(",", "."))
    return v if v > 0 else None


def from_csv(path: Path) -> list[tuple[str, str, float | None]]:
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        delim = ";" if sample.count(";") >= sample.count(",") else ","
        rows = list(csv.DictReader(fh, delimiter=delim))
    if not rows:
        return []
    cols = list(rows[0])
    url_c = next((c for c in cols if _URL_COL.match(c.strip())), None)
    name_c = next((c for c in cols if _NAME_COL.match(c.strip())), None)
    prices = [c for c in cols if _PRICE_COL.search(c)]
    price_c = next((c for c in prices if _PRICE_SITE.search(c)), prices[0] if prices else None)
    if not url_c or not price_c:
        raise SystemExit(f"в {path.name} не нашёл колонки URL/цены. Есть: {cols[:12]}")
    print(f"колонки фида: URL={url_c!r} | цена={price_c!r} | название={name_c!r}")
    out = []
    for r in rows:
        u = (r.get(url_c) or "").strip()
        if not u:
            continue
        out.append(((r.get(name_c) or "").strip() if name_c else "", u, num(r.get(price_c))))
    return out


def from_xml(path_or_url: str) -> list[tuple[str, str, float | None]]:
    if path_or_url.startswith(("http://", "https://")):
        import urllib.request
        with urllib.request.urlopen(path_or_url, timeout=120) as fh:
            data = fh.read()
    else:
        data = Path(path_or_url).read_bytes()
    root = ET.fromstring(data)
    out = []
    for off in root.iter("offer"):
        # url бывает и тегом, и атрибутом; имя — name или model
        u = (off.findtext("url") or off.get("url") or "").strip()
        if not u:
            continue
        nm = (off.findtext("name") or off.findtext("model") or "").strip()
        out.append((nm, u, num(off.findtext("price"))))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("feed", help="CSV/XML файл или ссылка на фид")
    ap.add_argument("--out", default=None, help=f"куда положить (по умолчанию {OURS_DIR})")
    ap.add_argument("--dry-run", action="store_true", help="только показать, ничего не писать")
    ap.add_argument("--only-feed", action="store_true",
                    help="не досыпать цены из текущего файла (по умолчанию досыпаются)")
    a = ap.parse_args()

    src = a.feed
    rows = from_csv(Path(src)) if src.lower().endswith(".csv") else from_xml(src)
    priced = [r for r in rows if r[2]]
    print(f"в фиде позиций: {len(rows):,}, из них с ценой: {len(priced):,}")
    if not priced:
        raise SystemExit("цен в фиде нет — файл не пишу")

    # Сверка с тем, что матчер читает сейчас: сколько цен изменилось и на сколько.
    cur = find_ours("products_export")
    if cur and Path(cur).exists():
        old = {}
        for row in csv.reader(open(cur, encoding="utf-8-sig", errors="replace"), delimiter=";"):
            if len(row) >= 3:
                p = num(row[2])
                if p:
                    old[slug(row[1])] = p
        new = {slug(u): p for _, u, p in priced}
        both = set(old) & set(new)
        changed = [(k, old[k], new[k]) for k in both if abs(new[k] - old[k]) > 0.5]
        print(f"\nтекущий файл цен: {Path(cur).name} ({len(old):,} позиций)")
        print(f"  общих позиций:   {len(both):,}")
        print(f"  цена изменилась: {len(changed):,}")
        if changed:
            up = [c for c in changed if c[2] > c[1]]
            med = sorted(abs(c[2] / c[1] - 1) for c in changed)[len(changed) // 2] * 100
            print(f"    подорожало {len(up):,} | подешевело {len(changed)-len(up):,} | медиана сдвига {med:.1f}%")
            for k, o, n in changed[:5]:
                print(f"    {k[:50]:52} {o:>12,.0f} -> {n:>12,.0f}")
        print(f"  только в фиде:   {len(set(new)-set(old)):,}")
        print(f"  только в старом: {len(set(old)-set(new)):,}  (останутся без цены)")

    if a.dry_run:
        print("\nпробный прогон, файл не записан")
        return 0

    out_rows = list(priced)
    if not a.only_feed and cur and Path(cur).exists():
        have = {slug(u) for _, u, _ in priced}
        kept = 0
        for row in csv.reader(open(cur, encoding="utf-8-sig", errors="replace"), delimiter=";"):
            if len(row) < 3 or slug(row[1]) in have:
                continue
            p = num(row[2])
            if p:
                out_rows.append((row[0], row[1], p)); kept += 1
        print(f"\nдосыпано из {Path(cur).name}: {kept:,} позиций, которых нет в фиде")

    outdir = Path(a.out) if a.out else OURS_DIR
    outdir.mkdir(parents=True, exist_ok=True)
    # Имя с пометкой feed: иначе перезаписали бы выгрузку Битрикса того же числа,
    # а она нужна как запасной источник цен. find_ours ищет по подстроке
    # "products_export" и берёт самый свежий по времени — этот файл и победит.
    dst = outdir / f"products_export_feed_{date.today():%Y%m%d}.csv"
    with open(dst, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        for nm, u, p in out_rows:
            w.writerow([nm, u, f"{p:.1f}"])
    print(f"-> {dst}  (всего {len(out_rows):,}: из фида {len(priced):,})")
    print("матчер возьмёт его сам: find_ours берёт самый свежий products_export_*")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
