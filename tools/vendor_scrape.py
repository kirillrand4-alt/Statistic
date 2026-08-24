"""Сбор каталогов С САЙТОВ ПРОИЗВОДИТЕЛЕЙ — цена и характеристики из первых рук.

Зачем отдельно от основного парсера. Тот ходит по шести агрегаторам и берёт то, что
продавец написал у себя. Здесь другая задача: заказчик просит цену и свойства именно
с сайта завода, и с каждого сайта — только его марку (berg-air.ru обслуживает сразу
BERG и ATOM, их надо развести).

Почему у каждого сайта свой адаптер, а не один универсальный разбор. Цена лежит
по-разному, и одного шаблона не хватает — это выяснилось дорого:
  * Berg (Bitrix + шаблон intec): цена НЕ в тексте страницы, а в дважды экранированном
    JSON внутри атрибута тега — data-data="{&quot;value&quot;:138478,&quot;display&quot;:
    &quot;138&amp;nbsp;478 руб.&quot;}". Поиск по сырому HTML её не видит («руб» закрыто
    как &quot;), а срезание тегов выбрасывает атрибут вместе с тегом. Ровно на этом я
    сначала объявил, что завод цен не публикует — вывод был неверный.
  * Sollant, Crossair, Dali: обычный Bitrix, цена в тексте карточки товара.
  * Xeleron, GMP: WooCommerce, цена в разметке WooCommerce.
Поэтому: определяем движок, пишем адаптер, и ОБЯЗАТЕЛЬНО сверяем 10 позиций с тем,
что видно на странице глазами. Без сверки адаптер не считается готовым.

    python tools/vendor_scrape.py berg          # один сайт
    python tools/vendor_scrape.py --list        # что уже поддержано
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

OUT = Path("/home/user/Statistic/vendor_data")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def get(url: str, timeout: int = 45, tries: int = 3) -> str:
    """Страница. Повтор с паузой: у части заводских сайтов первый запрос отдаёт пустое
    или обрывается — так у нас уже уехала целая перепись (sitemap отдавался через раз)."""
    for i in range(tries):
        r = subprocess.run(["curl", "-sS", "-m", str(timeout), "-L", "--compressed",
                            "-A", UA, url], capture_output=True, text=True, errors="replace")
        if r.stdout and len(r.stdout) > 800:
            return r.stdout
        time.sleep(1.5 * (i + 1))
    return ""


def text_of(page: str) -> str:
    """HTML -> текст построчно. script/style вырезаем до снятия тегов, иначе в текст
    попадают куски кода и ломают разбор пар «свойство — значение»."""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "\n", t))
    return re.sub(r"[ \t\xa0 ]+", " ", t)


def lines_of(page: str) -> list[str]:
    return [l.strip() for l in text_of(page).split("\n") if l.strip()]


# --- универсальный обход каталога ---------------------------------------------------------
_SKIP_URL = re.compile(r"/(filter|compare|basket|cart|search|login|auth|feed|page/\d+"
                       r"|zapchast\w*|raskhodnik\w*|maslo\w*|filtry)(/|$)|"
                       r"\.(jpg|jpeg|png|gif|webp|pdf|css|js|xml|zip)$", re.I)


def crawl_cards(base: str, roots, sect_re: str, max_pages: int = 900) -> list[str]:
    """Обход каталога вширь. Возвращает адреса страниц, похожих на карточку товара.

    Почему обход, а не sitemap: sitemap у заводских сайтов отдаётся через раз — в одном
    прогоне девять сайтов из десяти вернули пусто, и на этом уже уехала целая перепись.
    Обход медленнее, но повторяем: get() сам делает три попытки.

    Карточкой считаем страницу, где нашлась цена ИЛИ таблица характеристик с мощностью
    и давлением. Судить по одному виду адреса нельзя: у одних сайтов товар лежит на
    третьем уровне, у других на втором, а у части раздел и карточка неразличимы по пути."""
    seen, queue, cards = set(), list(roots), []
    host = urlparse(base).netloc.replace("www.", "")
    rx = re.compile(sect_re, re.I)
    while queue and len(seen) < max_pages:
        u = queue.pop(0)
        if u in seen or _SKIP_URL.search(urlparse(u).path):
            continue
        seen.add(u)
        page = get(u, 35, 2)
        if not page:
            continue
        if find_prices(page) or (re.search(r"мощност", page, re.I) and re.search(r"давлен", page, re.I)
                                 and re.search(r"<h1", page, re.I)):
            cards.append(u)
        for m in re.findall(r'href="([^"#?]+)"', page):
            v = urljoin(u, m).split("?")[0]
            if (urlparse(v).netloc.replace("www.", "") == host and rx.search(urlparse(v).path)
                    and v not in seen and v not in queue and not _SKIP_URL.search(urlparse(v).path)):
                queue.append(v)
    return cards


_RUB = re.compile(r"(\d[\d\s\xa0 ]{2,12})\s*(?:₽|руб)", re.I)
_ATTR_PRICE = re.compile(
    r'(?:itemprop="price"[^>]*content="|data-value="|"(?:value|price|PRICE)"\s*:\s*"?)(\d{3,9})')


def find_prices(page: str) -> list[int]:
    """Все суммы со страницы. ДВА прохода, и оба обязательны — на этом я ошибся дважды:

      * по тексту БЕЗ ТЕГОВ: у Xeleron цена свёрстана как
        <span class='arp_price_value'>1 923 075</span><span ...> руб </span>,
        то есть между числом и словом «руб» стоит разметка, и поиск по сырому HTML
        её не находит;
      * по атрибутам и JSON: у Berg цена живёт в data-data="{&quot;value&quot;:138478…}",
        а срезание тегов выбрасывает атрибут вместе с тегом.

    Один проход всегда пропускает половину сайтов. Санити-рамка 1 000…100 млн отсекает
    телефоны, годы и артикулы."""
    out: set[int] = set()
    for src in (text_of(html.unescape(page)), html.unescape(html.unescape(page))):
        for m in _RUB.findall(src):
            v = re.sub(r"\D", "", m)
            if v.isdigit():
                out.add(int(v))
    for m in _ATTR_PRICE.findall(html.unescape(page)):
        out.add(int(m))
    return sorted(v for v in out if 1000 <= v <= 100_000_000)


# --- адаптер: Bitrix + шаблон intec (Berg/Atom) -------------------------------------------
def berg_urls(base: str, section: str) -> list[str]:
    """Адреса карточек со страниц листинга.

    Цену НЕ берём из листинга, хотя она там есть: в data-data свойства исполнений
    закодированы идентификаторами справочника (P_DAVLENYE_BAR: "113" вместо «10 бар»),
    и расшифровка лежит отдельным блоком фильтра. С карточки и цена, и характеристики
    читаются как есть, поэтому идём по карточкам — медленнее, но без догадок."""
    out: list[str] = []
    for page_no in range(1, 41):
        page = get(f"{base}{section}?PAGEN_1={page_no}")
        if not page:
            break
        links = [l for l in dict.fromkeys(
                    re.findall(rf'href="({re.escape(section)}[^"?#]+/)"', page))
                 if l.rstrip("/") != section.rstrip("/") and "/filter/" not in l]
        fresh = [urljoin(base, l) for l in links if urljoin(base, l) not in out]
        if not fresh:                 # страница не принесла новых карточек — конец списка
            break
        out += fresh
    return out


_SPEC_SKIP = re.compile(r"^(—|-|·)$")


def berg_offers(page: str) -> list[dict]:
    """Торговые предложения карточки: у каждого СВОЯ цена и свои свойства.

    Ключевой момент, из-за которого первая версия адаптера дала неверные данные: на
    карточке ВК-11Р двадцать предложений (5 давлений x IP23/IP55 x с частотником и без)
    и ВОСЕМЬ разных цен — от 250 422 до 387 242 руб. Если брать одну цену из текста
    страницы и раздавать её всем исполнениям, отчёт получит цену базового исполнения
    там, где на самом деле стоит частотное, и сравнение поедет в разы.

    Свойства в offers закодированы идентификаторами справочника
    (P_DAVLENYE_BAR: "113"), а расшифровка лежит на той же странице отдельным блоком
    вида "113":{"id":113,"name":"8 бар"}. Собираем словарь и переводим.

    offers приходит СЛОВАРЁМ, не списком — обход по индексу молча падает."""
    t = html.unescape(html.unescape(page))
    names = {m.group(1): m.group(2) for m in
             re.finditer(r'"(\d+)"\s*:\s*\{"id":\1,"name":"([^"]*)"', t)}
    blob = None
    for m in re.finditer(r'data-data="([^"]+)"', page):
        try:
            d = json.loads(html.unescape(html.unescape(m.group(1))))
        except Exception:
            continue
        if isinstance(d, dict) and d.get("offers"):
            blob = d
            break
    if not blob:
        return []
    offers = blob["offers"]
    offers = list(offers.values()) if isinstance(offers, dict) else offers
    out, seen = [], set()
    for o in offers:
        price = next((b.get("base", {}).get("value") for b in (o.get("prices") or [])
                      if b.get("base", {}).get("value")), None)
        if not price:
            continue
        vals = {k: names.get(str(v), str(v)) for k, v in (o.get("values") or {}).items()}
        key = (price, tuple(sorted(vals.items())))
        if key in seen:          # карточка отдаёт блок дважды (десктоп + мобильная вёрстка)
            continue
        seen.add(key)
        out.append(dict(price=price, vals=vals))
    return out


def berg_card_common(page: str) -> tuple[str, dict, dict]:
    """(название, общие свойства, карта «давление -> производительность»).

    Производительность в offers не лежит, а она зависит от давления: у ВК-11Р это
    1.7 м3/мин на 8 бар и 0.9 на 16. Берём её из таблицы характеристик и раздаём
    предложениям по совпадению давления."""
    ls = lines_of(page)
    name = ""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    if m:
        name = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", m.group(1)))).strip()
    common, flow_by_bar, cur = {}, {}, {}
    try:
        i = next(k for k, l in enumerate(ls) if re.fullmatch(r"Характеристики", l, re.I))
    except StopIteration:
        return name, common, flow_by_bar
    k = i + 1
    while k < len(ls) - 2:
        key = ls[k]
        if re.search(r"руб\.|Доп\. товары|Описание|Отзывы|Заказать|наличии", key, re.I):
            break
        if not _SPEC_SKIP.fullmatch(ls[k + 1]):
            k += 1
            continue
        val, kl = ls[k + 2], key.lower()
        if kl.startswith(("модель", "мощност")):
            common[key] = val
        elif "давлен" in kl:
            cur = {"bar": re.sub(r"[^\d.,]", "", val).replace(",", ".")}
        elif "производительн" in kl and cur.get("bar"):
            flow_by_bar[cur["bar"]] = re.sub(r"[^\d.,]", "", val).replace(",", ".")
        k += 3
    return name, common, flow_by_bar


def run_berg(limit: int = 0):
    base, section = "https://berg-air.ru", "/catalog/vintovye-kompressory/"
    urls = berg_urls(base, section)
    print(f"  карточек в листинге: {len(urls)}")
    if limit:
        urls = urls[:limit]
    rows = []

    def one(u):
        page = get(u)
        name, common, flow = berg_card_common(page)
        return u, name, common, flow, berg_offers(page)

    with ThreadPoolExecutor(max_workers=5) as ex:
        for u, name, common, flow, offers in ex.map(one, urls):
            if not name:
                continue
            # berg-air.ru везёт две марки — разводим по названию товара
            brand = "ATOM" if re.search(r"atom|атом", name, re.I) else "BERG"
            kw = re.sub(r"[^\d.,]", "", next((v for k, v in common.items()
                                              if "мощност" in k.lower()), "")).replace(",", ".")
            model = next((v for k, v in common.items() if k.lower().startswith("модель")), "")
            for o in offers or [dict(price="", vals={})]:
                v = o["vals"]
                bar = re.sub(r"[^\d.,]", "", v.get("P_DAVLENYE_BAR", "")).replace(",", ".")
                vsd = v.get("P_S_CHASTOTNYM_PREOBRAZOVATILEM", "")
                rows.append(dict(
                    brand=brand, name=name, model=model, price=o["price"], url=u,
                    kw=kw, bar=bar, flow=flow.get(bar, ""),
                    ip=re.sub(r"\s+", "", v.get("P_IP_ZASHITA", "")),
                    vsd=vsd,
                    specs=json.dumps(v, ensure_ascii=False)))
    return rows



# --- адаптер: Bitrix (Sollant) ------------------------------------------------------------
_SOL_SECTIONS = ("/product/kompressornoe_oborudovanie/",
                 "/product/dizelnye_kompressory_sollant/",
                 "/product/bezmaslyanye_kompressory_sollant/")


def sitemap_urls(base: str) -> list[str]:
    """Все адреса из sitemap, включая вложенные карты. Отдельной функцией, потому что
    у части заводских сайтов sitemap отдаётся через раз — повторы делает get()."""
    urls: list[str] = []
    for f in ("sitemap.xml", "sitemap_index.xml"):
        x = get(urljoin(base, f), 30)
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", x)
        for sub in [l for l in locs if l.endswith(".xml")][:12]:
            locs += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", get(sub, 30))
        urls = [l for l in locs if not l.endswith(".xml")]
        if len(urls) > 20:
            break
    return list(dict.fromkeys(urls))


def sollant_urls(base: str) -> list[str]:
    """Адреса карточек: sitemap ПЛЮС обход листингов.

    Одного обхода листингов мало — постраничный цикл обрывался раньше времени и терял
    товары, которые видно на первой же странице каталога (SLT-5.5F с ценой 167 763 из
    скриншота заказчика в сбор не попал). Sitemap даёт полный список, листинги
    добирают то, чего в нём нет. Дешевле, чем гадать, какой из двух источников полон."""
    subs, out = list(_SOL_SECTIONS), []
    for sec in list(_SOL_SECTIONS):
        page = get(base + sec)
        subs += [l for l in re.findall(rf'href="({re.escape(sec)}[^"?#]+/)"', page)
                 if "/filter/" not in l]
    subs = list(dict.fromkeys(subs))
    for sec in subs:
        for page_no in range(1, 21):
            page = get(f"{base}{sec}?PAGEN_1={page_no}")
            if not page:
                break
            links = [l for l in dict.fromkeys(
                        re.findall(rf'href="({re.escape(sec)}[^"?#]+/)"', page))
                     if "/filter/" not in l and l.rstrip("/") != sec.rstrip("/")
                     # ТОВАР, а не подраздел: у карточки четыре сегмента пути
                     # (/product/<раздел>/<подраздел>/<товар>/), у подраздела — три.
                     # Без этой проверки в выборку попадали страницы разделов
                     # («Компрессоры 4-в-1 Sollant»), у них нет ни характеристик,
                     # ни цены предложения — только цена «от».
                     and len([x for x in l.strip("/").split("/") if x]) == 4
                     and l not in subs]
            fresh = [urljoin(base, l) for l in links if urljoin(base, l) not in out]
            if not fresh:
                break
            out += fresh
    # добор из sitemap: товар = четыре сегмента пути под /product/
    for u in sitemap_urls(base):
        pth = urlparse(u).path
        if (pth.startswith("/product/") and len([x for x in pth.strip("/").split("/") if x]) == 4
                and u not in out and "/filter/" not in pth):
            out.append(u)
    return out


def sollant_card(url: str, page: str) -> list[dict]:
    """Карточка -> строки по ТОРГОВЫМ ПРЕДЛОЖЕНИЯМ.

    Цена предложения в HTML карточки НЕ лежит: в data-json стоят только идентификаторы
    (ID, TREE, CAN_BUY), а цену сайт подставляет при выборе варианта. Но страница
    принимает ?oid=<ID> и отдаёт цену выбранного предложения в data-value — этого
    достаточно, браузер не нужен.

    Зачем вообще ходить по предложениям, а не взять цену «по умолчанию»: у SLT-5.5F
    винтовой блок Hanbell AC стоит 167 763, а Hanbell AB — 221 938, то есть плюс 32%
    при одинаковой мощности и давлении. «Модель винтового блока» — столбец шаблона
    заказчика, и одна цена на карточку сравнивала бы разные машины."""
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    name = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", h1.group(1)))).strip() if h1 else ""
    if not name:
        return []
    # расшифровка кодов свойств: data-treevalue="730_2906" + title="Рабочее давление (Бар): 8"
    vals = {(m.group(1), m.group(2)): (m.group(3).strip(), m.group(4).strip())
            for m in re.finditer(
                r'data-treevalue="(\d+)_(\d+)"[^>]*title="([^:"]+):\s*([^"]+)"', page)}
    m = re.search(r"data-json='(\[.*?\])'", page, re.S)
    try:
        offers = json.loads(m.group(1)) if m else []
    except Exception:
        offers = []
    # общие свойства карточки — те же тройки «свойство / — / значение», что у Berg
    ls, common = lines_of(page), {}
    for k in range(len(ls) - 2):
        if ls[k + 1] in ("—", "-") and re.search(
                r"мощност|привод|частотн|габарит|тип двигат|диаметр|ступен|охлажд|масл", ls[k], re.I):
            common.setdefault(ls[k].strip(), ls[k + 2].strip())
    rows = []
    for o in offers or [None]:
        if o is None:
            price = next(iter(re.findall(r'data-value="(\d{4,9})"', page)), "")
            props = {}
        else:
            pg = get(f"{url}?oid={o['ID']}")
            price = next(iter(re.findall(r'data-value="(\d{4,9})"', pg)), "")
            props = {}
            for pk, pv in (o.get("TREE") or {}).items():
                label, value = vals.get((pk.replace("PROP_", ""), str(pv)), (pk, str(pv)))
                props[label] = value
        g = lambda pat, src: next((v for k, v in src.items() if re.search(pat, k, re.I)), "")
        rows.append(dict(
            brand="SOLLANT", name=name,
            model=name.split()[-1] if name else "",
            price=price, url=url,
            kw=re.sub(r"[^\d.,]", "", g(r"мощност", common)).replace(",", "."),
            bar=re.sub(r"[^\d.,]", "", g(r"давлен", props)).replace(",", "."),
            flow=re.sub(r"[^\d.,]", "", g(r"производительн", props) or g(r"производительн", common)).replace(",", "."),
            ip=re.sub(r"\s+", "", g(r"защит", props) or g(r"защит", common)),
            vsd=g(r"частотн", common),
            specs=json.dumps({**common, **props}, ensure_ascii=False)))
    return rows


def run_sollant(limit: int = 0):
    base = "https://sollant-rus.ru"
    urls = sollant_urls(base)
    print(f"  карточек в каталоге: {len(urls)}")
    if limit:
        urls = urls[:limit]
    rows = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        for r in ex.map(lambda u: sollant_card(u, get(u)), urls):
            rows += r
    return rows



# --- адаптер: WooCommerce (GMP) -----------------------------------------------------------
_WOO_AMOUNT = re.compile(r'woocommerce-Price-amount[^>]*>(?:<bdi>)?([\d\s\xa0 ]+)')
_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TD = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)


def woo_price(page: str) -> str:
    """Цена САМОГО товара — первая сумма внутри блока summary entry-summary.

    На карточке GMP пять сумм: своя и четыре из блока «похожие товары». Брать первую
    попавшуюся по странице нельзя — у GM 2,2-10-100A BOX это дало бы 377 695 вместо
    122 305, то есть цену чужой машины."""
    m = re.search(r'class="[^"]*summary entry-summary[^"]*"', page)
    if not m:
        return ""
    seg = page[m.start():m.start() + 6000]
    a = _WOO_AMOUNT.search(seg)
    return re.sub(r"\D", "", a.group(1)) if a else ""


_ATTR_LI = re.compile(
    r'custom-attrs-label"[^>]*>(.*?)</p>\s*<p class="custom-attrs-value"[^>]*>(.*?)</p>', re.S)


def attr_specs(page: str) -> dict:
    """Пары из блока краткого описания (ul.custom-attrs-list).

    У GMP мощность, производительность и частотный привод лежат ТОЛЬКО здесь, а
    давление, IP, ресивер и осушитель — только в таблице ниже. Читать надо оба места:
    по одной таблице половина карточек уходит без кВт вовсе.

    Ограничиваемся блоком summary САМОГО товара: точно такой же список атрибутов есть
    у каждого «похожего товара» ниже по странице, и без границы значения перетирались
    чужими — GM 11R-16 (11 кВт) получал 185 кВт от соседа по блоку."""
    m = re.search(r'class="[^"]*summary entry-summary[^"]*"', page)
    if not m:
        return {}
    seg = page[m.start():m.start() + 6000]
    return {re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", k))).strip():
            re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", v))).strip()
            for k, v in _ATTR_LI.findall(seg)}


def table_specs(page: str) -> dict:
    """Все пары «ключ / значение» из таблиц карточки."""
    out = {}
    for tr in _TR.findall(page):
        cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in _TD.findall(tr)]
        if len(cells) == 2 and cells[0] and cells[1] and len(cells[0]) < 60:
            out.setdefault(cells[0], cells[1])
    return out


def run_gmp(limit: int = 0):
    base = "https://gmp.energy/"
    # Из sitemap берём только КОМПРЕССОРЫ: там же лежат винтовые блоки, масла и
    # запчасти («vintovoy-blok-baosi-...»), и по слову «kompressor» они не отсеиваются.
    urls = [u for u in sitemap_urls(base)
            if re.search(r"/product/[^/]*kompressor", u, re.I)
            and not re.search(r"blok|maslo|filtr|zapchast|remkomplekt|osushitel|resiver", u, re.I)]
    print(f"  карточек-компрессоров: {len(urls)}")
    if limit:
        urls = urls[:limit]
    rows = []

    def one(u):
        c = get(u)
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", c, re.S)
        name = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", h1.group(1)))).strip() if h1 else ""
        return u, name, woo_price(c), {**table_specs(c), **attr_specs(c)}

    with ThreadPoolExecutor(max_workers=6) as ex:
        for u, name, price, sp in ex.map(one, urls):
            if not name:
                continue
            g = lambda pat: next((v for k, v in sp.items() if re.search(pat, k, re.I)), "")
            rows.append(dict(
                brand="GMP", name=name, model=name.replace("Винтовой компрессор GMP", "").strip(),
                price=price, url=u,
                kw=re.sub(r"[^\d.,]", "", g(r"мощност")).replace(",", "."),
                bar=re.sub(r"[^\d.,]", "", g(r"давлен")).replace(",", "."),
                flow=re.sub(r"[^\d.,]", "", g(r"производительн")).replace(",", "."),
                ip=re.sub(r"\s+", "", g(r"степень защит")),
                vsd=g(r"частотн"),
                specs=json.dumps(sp, ensure_ascii=False)))
    return rows



# --- адаптер: Xeleron (таблицы модельного ряда, БЕЗ цен) -----------------------------------
_XEL_SECTIONS = ("vintovye-kompressory/", "vintovye-kompressory-zpma/",
                 "vintovye-kompressory-dry-tank/", "bezmaslyanye-kompressory/",
                 "bezmaslyanye-kompressory-suhogo-szhatiya/")


def xeleron_card_prices(base: str, secs) -> dict:
    """Цены с карточек товаров: модель -> цена.

    ОТЗЫВ ПРЕЖНЕГО ВЫВОДА. Сначала я записал, что у Xeleron нет ни карточек, ни цен, —
    неверно оба раза. Карточки есть и со страниц разделов на них СТОЯТ ссылки; я
    посмотрел первые четыре по алфавиту (конденсатоотводчики, масла) и сделал вывод по
    ним. Цена на карточке свёрстана как <span class='arp_price_value'>1 923 075</span>
    <span> руб </span> — между числом и словом стоит разметка, и поиск по сырому HTML
    её не видит (см. find_prices, там теперь оба прохода).

    Карточек ровно 15, цена стоит на трёх (Z7.5A, Z10A, Z175A). Остальные 73 модели
    из таблиц цены на сайте не имеют: подстановка адресов по образцу даёт честный 404,
    скрытых страниц нет — проверено по коду ответа, а не по факту «страница открылась»."""
    out = {}
    cards = set()
    for sec in secs:
        page = get(base + sec)
        for m in re.findall(
                r'href="(https://xelerone\.com/kompressornoe-oborudovanie/[^"?#]+/[^"?#]+/)"', page):
            if not m.rstrip("/").endswith(tuple(x.strip("/") for x in secs)):
                cards.add(m)
    for u in sorted(cards):
        page = get(u)
        pr = find_prices(page)
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
        nm = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", h1.group(1))).strip() if h1 else ""
        m = re.search(r"\b([A-ZА-Я]{1,4}[\d.,]+\w*)\b", nm)
        if pr and m:
            out[m.group(1).upper().replace(",", ".")] = (pr[0], u)
    return out


def run_xeleron(limit: int = 0):
    """Модельный ряд Xeleron из таблиц разделов + цены с тех карточек, где они есть.

    Таблица устроена так: одна строка = модель, а производительность перечислена
    ЧЕТЫРЬМЯ значениями под четыре давления («1.3 1.2 1.0 0.8» для «7 8 10 12»).
    Разворачиваем в отдельные строки по давлениям — иначе сцепка по шаблону заказчика
    (точное совпадение давления) не сработает вовсе.

    Прочерк вместо числа — значит на этом давлении модель не выпускается: строку не
    создаём, чтобы не выдумывать несуществующее исполнение."""
    base = "https://xelerone.com/kompressornoe-oborudovanie/"
    prices = xeleron_card_prices(base, _XEL_SECTIONS)
    print(f"  карточек с ценой: {len(prices)}")
    rows = []
    for sec in _XEL_SECTIONS:
        page = get(base + sec)
        if not page:
            continue
        for tbl in re.findall(r"<table.*?</table>", page, re.S):
            trs = re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S)
            grid = [[re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)] for tr in trs]
            grid = [g for g in grid if g]
            if not grid or not re.search(r"модель", grid[0][0], re.I):
                continue
            hdr = grid[0]
            col = lambda pat: next((i for i, h in enumerate(hdr) if re.search(pat, h, re.I)), None)
            # «Произво-дительность» — в шапке таблицы слово разорвано ДЕФИСОМ переноса,
            # и шаблон «производ» её не находит (как и «Присоеди- нение»). Ищем по корню.
            i_fl, i_bar, i_kw = col(r"произв"), col(r"давлен"), col(r"мощност")
            i_dim, i_we = col(r"размер|габарит"), col(r"масса|вес")
            for g in grid[1:]:
                if len(g) < len(hdr) or not g[0]:
                    continue
                bars = re.findall(r"\d+(?:[.,]\d+)?", g[i_bar]) if i_bar is not None else []
                flows = re.split(r"\s+", g[i_fl].strip()) if i_fl is not None else []
                pairs = list(zip(bars, flows)) if len(bars) == len(flows) else [
                    (bars[0] if bars else "", flows[0] if flows else "")]
                for bar, fl in pairs:
                    if fl in ("-", "—", ""):   # на этом давлении модели нет
                        continue
                    pr, purl = prices.get(g[0].upper().replace(",", "."), ("", ""))
                    rows.append(dict(
                        brand="XELERON", name=f"Xeleron {g[0]}", model=g[0],
                        price=pr, url=purl or (base + sec),
                        kw=(g[i_kw].replace(",", ".") if i_kw is not None else ""),
                        bar=bar.replace(",", "."), flow=fl.replace(",", "."),
                        ip="", vsd="",
                        specs=json.dumps({h: g[i] for i, h in enumerate(hdr) if i < len(g)},
                                         ensure_ascii=False)))
    return rows



# --- универсальный адаптер: раздел -> постраничный листинг -> карточка ---------------------
def main_price(page: str) -> str:
    """Цена САМОГО товара — ближайшая к заголовку h1.

    Обобщение того, что пришлось делать руками для GMP: на карточке почти всегда есть
    ещё цены — блок «похожие товары», «с этим покупают», аксессуары. Брать первую по
    странице нельзя (GM 2,2-10-100A BOX получал 377 695 вместо 122 305), а привязка к
    имени CSS-класса не переносится с сайта на сайт. Расстояние до h1 переносится."""
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    if not h1:
        return ""
    title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", h1.group(1)))).strip()
    # Ищем в ТЕКСТЕ без тегов: в сыром HTML между числом и словом «руб» может лежать что
    # угодно, и гибкий шаблон ловил мусор — на карточке ET он взял 7342 из
    # id="bx_117848907_7342" вместо цены 207 303.
    t = text_of(html.unescape(page))
    anchor = t.find(title[:40]) if title else 0
    if anchor < 0:
        anchor = 0
    best, bestd = "", 10 ** 9
    for m in re.finditer(r"(\d[\d ]{2,12})\s*(?:₽|руб)", t, re.I):
        v = re.sub(r"\D", "", m.group(1))
        if not v.isdigit() or not (1000 <= int(v) <= 100_000_000):
            continue
        d = abs(m.start() - anchor)
        if d < bestd:
            best, bestd = v, d
    if best:
        return best
    # запасной путь — цена в атрибуте/JSON (так её отдаёт Berg, где текста с «руб» нет)
    for m in re.finditer(r'itemprop="price"[^>]*content="(\d{3,9})"|data-value="(\d{4,9})"',
                         html.unescape(page)):
        v = next((g for g in m.groups() if g), "")
        if v.isdigit() and 1000 <= int(v) <= 100_000_000:
            return v
    return ""


def listing_urls(base: str, sections, page_param: str = "PAGEN_1", max_pages: int = 60,
                 card_re: str | None = None) -> list[str]:
    """Адреса карточек: по каждому разделу идём постранично, пока приходят новые ссылки.

    card_re нужен там, где карточка лежит НЕ под адресом раздела: у Ironmac раздел
    /catalog/vintovye_kompressory/, а товары — /catalog/product/ic_15_8_digi_wifi/.
    Без него из семи страниц листинга не находилось ни одного товара."""
    out: list[str] = []
    pat = card_re or None
    for sec in sections:
        for n in range(1, max_pages + 1):
            page = get(f"{base}{sec}?{page_param}={n}")
            if not page:
                break
            # Ссылки бывают и относительные, и абсолютные: Kraftmachine пишет
            # href="https://kraftmachine.ru/catalog/...", и шаблон только под «/catalog/»
            # не находил на странице в 285 КБ ни одной ссылки.
            raw = (re.findall(pat, page) if pat
                   else re.findall(rf'href="((?:https?://[^"]*?)?{re.escape(sec)}[^"?#]+/)"', page))
            links = [l for l in dict.fromkeys(raw)
                     if l.rstrip("/") != sec.rstrip("/") and not _SKIP_URL.search(l)]
            fresh = [urljoin(base, l) for l in links if urljoin(base, l) not in out]
            if not fresh:
                break
            out += fresh
    return out


def parse_generic(url: str, page: str, brand: str) -> dict | None:
    """Карточка -> строка. Характеристики берём и из таблиц, и из пар «ключ / значение»
    подряд идущими строками: часть сайтов верстает их div'ами, а не таблицей."""
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    name = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", h1.group(1)))).strip() if h1 else ""
    if not name:
        return None
    sp = dict(table_specs(page))
    ls = lines_of(page)
    try:
        i = next(k for k, l in enumerate(ls) if re.fullmatch(r"Характеристики|Технические характеристики", l, re.I))
        for k in range(i + 1, min(i + 80, len(ls) - 1), 2):
            key, val = ls[k], ls[k + 1]
            if len(key) < 60 and val and not re.fullmatch(r"[—\-]", val):
                sp.setdefault(key, val)
    except StopIteration:
        pass
    g = lambda pat: next((v for k, v in sp.items() if re.search(pat, k, re.I)), "")
    return dict(brand=brand, name=name, model="", price=main_price(page), url=url,
                kw=re.sub(r"[^\d.,]", "", g(r"мощност")).replace(",", "."),
                bar=re.sub(r"[^\d.,]", "", g(r"давлен")).replace(",", "."),
                flow=re.sub(r"[^\d.,]", "", g(r"произв\w*дительн")).replace(",", "."),
                ip=re.sub(r"\s+", "", g(r"защит")), vsd=g(r"частотн"),
                specs=json.dumps(sp, ensure_ascii=False))


def make_runner(base: str, sections, brand: str, page_param: str = "PAGEN_1",
                card_re: str | None = None):
    def run(limit: int = 0):
        urls = listing_urls(base, sections, page_param, card_re=card_re)
        print(f"  карточек в листинге: {len(urls)}")
        if limit:
            urls = urls[:limit]
        rows = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            for r in ex.map(lambda u: parse_generic(u, get(u), brand), urls):
                if r:
                    rows.append(r)
        return rows
    return run



# --- адаптер: Kraftmachine (разбор ЛИСТИНГА, карточки не нужны) ----------------------------
_KM_CARD = re.compile(r'<h3 class="card-name">\s*<a href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_KM_CH = re.compile(r"<li>\s*<span>([^<]{2,60}?):?</span>\s*<span>[^<]*</span>\s*<span>\s*([^<]*?)\s*</span>", re.S)


def run_kraftmachine(limit: int = 0):
    """Kraftmachine отдаёт весь товар прямо в листинге, и ходить по карточкам не нужно.

    Почему пришлось делать отдельный адаптер, а не общий: ссылка на товар записана
    ОТНОСИТЕЛЬНО, без ведущей косой — href="catalog/vintovye-.../km11-10pv-...", и
    универсальный сборщик, ищущий «/catalog/…», не находил на странице в 400 КБ ни
    одной ссылки. Зато в листинге есть микроразметка: <meta itemprop="price"
    content="354003"> и <meta itemprop="name" content="КМ11-10пВ (KMV01110PB23JH1)">,
    плюс ul.card-characteristics с производительностью, мощностью и классом защиты.

    Пагинация здесь ?page=N, а не PAGEN_1."""
    base = "https://kraftmachine.ru/"
    secs = ["catalog/vintovye-maslozapolnennye-kompressory/", "catalog/bezmaslyanye-kompressory/",
            "catalog/dizelnye-kompressory/", "catalog/bezmaslyanye-bustery/"]
    rows, seen = [], set()
    for sec in secs:
        for n in range(1, 41):
            page = get(f"{base}{sec}?page={n}")
            if not page:
                break
            cards = list(_KM_CARD.finditer(page))
            if not cards:
                break
            new = 0
            for i, m in enumerate(cards):
                seg = page[m.end():cards[i + 1].start() if i + 1 < len(cards) else m.end() + 4000]
                url = urljoin(base, m.group(1))
                if url in seen:
                    continue
                seen.add(url); new += 1
                name = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", m.group(2)))).strip()
                # content="0" у этого сайта означает «цены нет» (товар под заказ), а не
                # ноль рублей: пустая ячейка честнее нуля, который попадёт в MIN и
                # обнулит всё сравнение.
                pm = re.search(r'itemprop="price"\s+content="([1-9]\d{2,})"', seg)
                nm2 = re.search(r'itemprop="name"\s+content="([^"]+)"', seg)
                sp = {k.strip(): v.strip() for k, v in _KM_CH.findall(seg)}
                g = lambda pat: next((v for k, v in sp.items() if re.search(pat, k, re.I)), "")
                rows.append(dict(
                    brand="KRAFTMACHINE", name=name,
                    model=(nm2.group(1) if nm2 else ""), price=(pm.group(1) if pm else ""),
                    url=url,
                    kw=re.sub(r"[^\d.,]", "", g(r"мощност")).replace(",", "."),
                    bar="", flow=re.sub(r"[^\d.,]", "", g(r"производительн")).replace(",", "."),
                    ip=re.sub(r"\s+", "", g(r"защит")), vsd="",
                    specs=json.dumps(sp, ensure_ascii=False)))
            if not new:
                break
    return rows


ADAPTERS = {"berg": ("berg-air.ru (BERG + ATOM)", run_berg),
            "sollant": ("sollant-rus.ru (SOLLANT)", run_sollant),
            "gmp": ("gmp.energy (GMP)", run_gmp),
            "xeleron": ("xelerone.com (XELERON)", run_xeleron),
            # Разделы перечислены явно, а не ищутся автоматически: у каждого сайта своя
            # раскладка, и «взять всё, где в адресе есть kompressor» тянет запчасти,
            # масла и услуги. Список короткий и меняется раз в год — дешевле держать его
            # здесь, чем разбирать мусор в отчёте.
            "kraftmachine": ("kraftmachine.ru (KRAFTMACHINE)", run_kraftmachine),
            "magnus": ("magnus-prom.ru (MAGNUS)", make_runner(
                "https://magnus-prom.ru",
                ["/product/vintovye_kompressory/", "/product/porshnevye-kompressory/",
                 "/product/tsentrobezhnye-kompressory1/"], "MAGNUS")),
            "hansmann": ("hansmann.ru (HANSMANN)", make_runner(
                "https://hansmann.ru",
                ["/catalog/vintovye_kompressory_hansmann_serii_rs/",
                 "/catalog/vintovye_kompressory_hansmann_serii_rsa/",
                 "/catalog/vintovye_kompressory_hansmann_serii_rse_s_chastotnym_preobazovatelem/",
                 "/catalog/dizelnye_kompressory_hansmann/"], "HANSMANN")),
            "zif": ("zif-kompressor.ru (ЗИФ)", make_runner(
                "https://zif-kompressor.ru",
                ["/catalog/vintovye-kompressory/", "/catalog/dizelnye-kompressory/",
                 "/catalog/kompressory-zif-sve/", "/catalog/rudnichnye-kompressory-zif-rn/",
                 "/catalog/vzryvozashchishchennye-kompressory/",
                 "/catalog/kompressory-dlya-burovykh/"], "ЗИФ")),
            "ironmac": ("ironmac-kompressor.com (IRONMAC)", make_runner(
                "https://ironmac-kompressor.com",
                ["/catalog/vintovye_kompressory/"], "IRONMAC",
                card_re=r'href="(/catalog/product/[^"?#]+/)"')),
            "exelute": ("exelute.ru (EXELUTE)", make_runner(
                "https://exelute.ru",
                ["/product/odnostupenchatye-vintovye-kompressory/",
                 "/product/dvukhstupenchatye-vintovye-kompressory/",
                 "/product/vintovye-kompressory-bezmaslyannye/",
                 "/product/dizelnye-kompressory/",
                 "/product/kompressory-dlya-lazernoy-rezki/"], "EXELUTE")),
            "et": ("et-compressors.ru (ET)", make_runner(
                "https://et-compressors.ru",
                ["/catalog/vintovye_kompressory/", "/catalog/spiralnye_kompressory/",
                 "/catalog/peredvizhnye_kompressory/"], "ET"))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("site", nargs="?", choices=sorted(ADAPTERS))
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="снять не больше N товаров (проба)")
    a = ap.parse_args()
    if a.list or not a.site:
        for k, (t, _) in sorted(ADAPTERS.items()):
            print(f"  {k:<14} {t}")
        return 0
    title, fn = ADAPTERS[a.site]
    print(f"{a.site}: {title}")
    rows = fn(a.limit)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{a.site}.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["brand", "name", "model", "price", "url", "kw", "bar",
                                           "flow", "ip", "vsd", "specs"], delimiter=";")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    have = sum(1 for r in rows if r["price"])
    print(f"  строк {len(rows)} | с ценой {have} | -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
