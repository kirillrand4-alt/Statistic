"""Точечный добор карточек конкурента, которые не забрал основной парсер.

Зачем. Проверка агентами 19.08 показала: у pnevmo-sklad в разделе Coaire на сайте
186 товаров, а в снимке 48; у Ultratech — 85 против 26. Страницы-листинги в дампе
парсера ЕСТЬ, а товары с них не забраны, то есть дело не в обходе, а в сборе.
Наши 126 карточек Coaire AS и 12 Ultratech UBD из-за этого не могут найти пару в
принципе: сравнивать не с чем.

Почему отдельный инструмент, а не правка парсера. Основной парсер ходит по sitemap
и фильтрует подкатегории (monitor/scrapers/pnevmo_sklad.py). Что именно там теряется
— вопрос к его логам, а они на боевом сервере. Этот инструмент решает другую задачу:
добрать конкретный раздел здесь и сейчас, чтобы матчинг перестал быть слепым, и
получить эталон, с которым можно сверить парсер.

pnevmo-sklad отдаёт 403 на любой запрос не из России (проверено 19.08: пять остальных
площадок открываются с той же машины, эта — нет; браузерные заголовки не помогают,
фильтр по IP). Зеркало r.jina.ai пропускает ровно один запрос, дальше тоже 403 —
для добора 199 карточек не годится.

Поэтому инструмент рассчитан на боевой сервер, где у парсера есть рабочий прокси:
там нужен ключ --direct, и зеркало не участвует вовсе. Разовый сбор без прокси
делается иначе — через WebFetch агентами (он ходит по другому маршруту и проходит);
так и был получен первый добор 19.08.

Результат пишется в CSV того же формата, что выдаёт парсер (site, brand, name, price,
specs-JSON, product_url…), и кладётся рядом с остальными выгрузками — load_comp_all
читает каталог целиком, так что добор подхватывается без правок матчера.

    python tools/dobor_konkurenta.py --razdel coaire ultratech -o parser_all/dobor.csv
    python tools/dobor_konkurenta.py --razdel coaire --limit 5 -o /tmp/proba.csv

Режим --iz-json собирает тот же CSV из файлов, снятых через WebFetch: массив объектов
{"url","name","price","price_on_request","sku","specs":{"Ключ":"значение"}}, где specs —
характеристики ДОСЛОВНО со страницы, без перевода и пересчёта (матчер разбирает их сам,
теми же правилами, что и остальной снимок).

    python tools/dobor_konkurenta.py --iz-json dobor_coaire1.json dobor_coaire2.json \
        -o parser_all/dobor_20260819.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MIRROR = "https://r.jina.ai/"
BASE = "https://www.pnevmo-sklad.ru"

# Разделы, где доказан недобор. Ключ — как писать в --razdel, значение — страницы
# листингов на сайте. Список конкретный, а не «весь каталог»: инструмент точечный.
RAZDELY = {
    "coaire": [
        "/shop/oborudovanie/vintovye_kompressory/coaire/as/",
        "/shop/oborudovanie/vintovye_kompressory/coaire/as_v/",
        "/shop/oborudovanie/vintovye_kompressory/coaire/as_k_kv/",
        "/shop/oborudovanie/vintovye_kompressory/coaire/as_a_p/",
        "/shop/oborudovanie/vintovye_kompressory/coaire/af/",
    ],
    "ultratech": [
        "/shop/oborudovanie/vintovye_kompressory/ultratech/",
    ],
}

COLS = ["site", "brand", "series", "name", "model", "sku", "price", "old_price",
        "discount_pct", "currency", "price_on_request", "price_raw", "availability",
        "series_status", "replacement_model", "specs", "category_path",
        "product_url", "image_url", "normalized_key", "scraped_at"]

_PROD = re.compile(r"\((https://www\.pnevmo-sklad\.ru/shop/oborudovanie/[^)\s]+)\)")
# Зеркало отдаёт характеристики строками таблицы «| Ключ | Значение |» либо
# «Ключ: Значение». Берём оба вида — разные шаблоны страниц у одного сайта.
_ROW = re.compile(r"^\|\s*([^|]{2,40}?)\s*\|\s*([^|]{1,60}?)\s*\|\s*$", re.M)
_KV = re.compile(r"^([А-Яа-яA-Za-z][^:\n]{2,40}):\s+([^\n]{1,60})$", re.M)
_PRICE = re.compile(r"([\d\s  ]{4,15})\s*(?:₽|руб)")


def fetch(url: str, direct: bool, tries: int = 3) -> str:
    """Текст страницы. Через зеркало, если сайт закрыт для этой машины."""
    target = url if direct else MIRROR + url
    for i in range(tries):
        r = subprocess.run(["curl", "-sS", "-m", "120", "--compressed", "-A",
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
                            target], capture_output=True, text=True)
        if r.stdout and len(r.stdout) > 1500 and "403 Forbidden" not in r.stdout[:400]:
            return r.stdout
        time.sleep(3 * (i + 1))
    return ""


def product_urls(page: str, listing: str) -> list[str]:
    """Ссылки на карточки со страницы листинга (сам листинг и разделы отбрасываем)."""
    out, seen = [], set()
    for u in _PROD.findall(page):
        u = u.split("?")[0].rstrip("/")
        # карточка — последний сегмент со слагом товара; у разделов он короткий
        # и совпадает с одним из путей листингов
        if u.endswith(listing.rstrip("/")) or len(u.rsplit("/", 1)[-1]) < 12:
            continue
        if u not in seen:
            seen.add(u); out.append(u)
    return out


def parse_card(url: str, page: str) -> dict | None:
    """Страница карточки -> строка в формате парсера. None, если спек не нашлось."""
    title = re.search(r"^Title:\s*(.+)$", page, re.M)
    name = (title.group(1) if title else "").split(" - купить")[0].strip()
    if not name:
        return None
    specs = {}
    for k, v in _ROW.findall(page) + _KV.findall(page):
        k, v = k.strip(), v.strip()
        if not k or not v or k.lower().startswith(("изображ", "image", "http")):
            continue
        if k not in specs and len(specs) < 40:
            specs[k] = v
    if len(specs) < 3:
        return None
    m = _PRICE.search(page)
    price = re.sub(r"\D", "", m.group(1)) if m else ""
    brand = specs.get("Бренд") or specs.get("Производитель") or ""
    row = {c: "" for c in COLS}
    row.update(site="pnevmo-sklad.ru", brand=brand, name=name,
               model=specs.get("Артикул", ""), sku=specs.get("Артикул", ""),
               price=price, currency="RUB",
               availability=("В наличии" if "в наличии" in page.lower() else ""),
               specs=json.dumps(specs, ensure_ascii=False), product_url=url,
               normalized_key=re.sub(r"[^A-Za-zА-Яа-я0-9]", "", (brand + name)).upper(),
               scraped_at=datetime.now(timezone.utc).isoformat())
    return row


def warn_name(out: Path) -> None:
    """Матчер ищет файлы по маске prices_<дата>_<время>.csv (scrape_files._TS). Файл с
    другим именем он просто не увидит — собранные 241 карточка так и не подхватились,
    пока файл назывался dobor_20260819.csv."""
    if not re.match(r"(?:all_)?prices(?:_checked)?_\d{8}", out.name):
        print("  ВНИМАНИЕ: имя файла не подходит под маску scrape_files — матчер его не "
              "увидит. Переименуйте в prices_<ГГГГММДД>_<ЧЧММСС>.csv", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--razdel", nargs="+", choices=sorted(RAZDELY))
    ap.add_argument("--iz-json", nargs="+", metavar="FILE",
                    help="собрать CSV из JSON-файлов, снятых WebFetch (см. шапку)")
    # Имя по умолчанию подходит под маску, которую ищет scrape_files (prices_<дата>_<время>).
    # Файл с любым другим именем матчер просто не увидит: собранные 241 карточка сначала
    # так и не подхватились, пока файл назывался dobor_20260819.csv.
    ap.add_argument("-o", "--out",
                    default=f"prices_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.csv")
    ap.add_argument("--limit", type=int, default=0, help="снять не больше N карточек")
    ap.add_argument("--direct", action="store_true",
                    help="ходить напрямую, без зеркала (на сервере с рабочим прокси)")
    a = ap.parse_args()

    if a.iz_json:
        rows = []
        for f in a.iz_json:
            for c in json.load(open(f, encoding="utf-8")):
                specs = c.get("specs") or {}
                if len(specs) < 3:
                    continue
                brand = specs.get("Бренд") or specs.get("Производитель") or ""
                row = {k: "" for k in COLS}
                row.update(site="pnevmo-sklad.ru", brand=brand, name=c.get("name", ""),
                           model=c.get("sku", ""), sku=c.get("sku", ""),
                           price=re.sub(r"\D", "", str(c.get("price") or "")),
                           currency="RUB",
                           price_on_request="1" if c.get("price_on_request") else "",
                           availability=c.get("availability", ""),
                           specs=json.dumps(specs, ensure_ascii=False),
                           product_url=c.get("url", ""),
                           normalized_key=re.sub(r"[^A-Za-zА-Яа-я0-9]", "",
                                                 brand + c.get("name", "")).upper(),
                           scraped_at=datetime.now(timezone.utc).isoformat())
                rows.append(row)
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=COLS)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"из JSON собрано {len(rows)} карточек -> {out.resolve()}")
        warn_name(out)
        return 0
    if not a.razdel:
        sys.exit("нужен --razdel или --iz-json")

    todo: list[str] = []
    for r in a.razdel:
        for listing in RAZDELY[r]:
            page = fetch(BASE + listing, a.direct)
            if not page:
                print(f"  {listing}: не открылась", file=sys.stderr); continue
            urls = product_urls(page, listing)
            print(f"  {listing}: карточек {len(urls)}")
            todo += urls
    todo = list(dict.fromkeys(todo))
    if a.limit:
        todo = todo[:a.limit]
    print(f"к добору: {len(todo)}\n")

    rows, skip = [], 0
    for i, u in enumerate(todo, 1):
        r = parse_card(u, fetch(u, a.direct))
        if r:
            rows.append(r)
        else:
            skip += 1
        if i % 20 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)} | снято {len(rows)} | без характеристик {skip}",
                  flush=True)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\nзаписано {len(rows)} карточек -> {out.resolve()}")
    warn_name(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
