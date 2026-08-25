"""Аналоги Enger по ШАБЛОНУ СВОЙСТВ заказчика — отчёт в раскладке присланного образца.

Отличие от `enger_analogi.py` (он остаётся как был):

  * конкуренты берутся НЕ с площадок-перекупщиков, а с сайтов производителей
    (`vendor_data/*.csv`, собрано `tools/vendor_scrape.py`), и с каждого сайта — только
    его бренд. Это прямое требование заказчика: цена нужна заводская;
  * сцепка не по нашим допускам (±3% давление, ±5% производительность), а по 13
    свойствам шаблона с ТОЧНЫМ совпадением — допусков нет;
  * десять «зелёных» свойств шаблона при молчании сайта заменяются первым значением
    столбца, как и просил заказчик. Разбор цветов и обоснование — в `tools/enger_specs.py`.

Листа со сводкой два, и это не дубль:

  * «Сводная» — раскладка, формулы и цвета образца один в один: строка = модель Enger,
    две строки на модель (во второй следующий по цене аналог бренда), сцепка по 8 бар с
    откатом по ряду, если такого давления у серии нет (LUF идёт на 7,5 / 8,6 / 10,4);
  * «Сводная по барам» — строка на каждое давление. Нужна потому, что у 86 семейств из
    460 цена по барам реально различается: LC-18,5DFRE-500 стоит 921 086 / 1 173 338 /
    1 466 672 ₽, и модельная строка образца показывает одну цену из трёх. (Прежний
    пример «BS-5,5BFR-250 на 8 бар 312 125 ₽» был артефактом прайс-фида — в каталожной
    выгрузке у этой модели одна цена 327 844 ₽ на все пять баров.)

    python enger_analogi_shablon.py            # боевой прогон
    python enger_analogi_shablon.py --proba    # только 10 моделей из образца, с разбором

Данные: OURS_DIR (наш каталог), vendor_data/ (сайты производителей).
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent          # скрипт зовут и из другой рабочей папки
sys.path[:0] = [str(_HERE), str(_HERE / "tools")]

import openpyxl
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.styles.colors import Color
from openpyxl.utils import get_column_letter

from brand_spec_review import load_ours_all
from scrape_files import find_ours
from atlas_need_specs import load_universe
from enger_specs import (PROPS, DEFAULTS, KW_LADDER, enger_block, enger_cooling, ip_classes,
                         bar_bucket, bar_classes, kw_value, num,
                         pick, yesno, yesno_or_model, from_name, block_class, motor_class,
                         cooling, stages)

VENDORS = Path("/home/user/Statistic/vendor_data")
OUT = "/home/user/Statistic/Enger_analogi_shablon.xlsx"

# Порядок брендов — как в образце («Сводная», тройки колонок с AA). Remeza оставлен на
# своём месте пустым: remeza.com — сайт завода, цен там нет ни в разделах, ни в карточках
# (проверено 24.08 обходом /catalog/compressors/screw_compressors/), а брать её цену с
# площадки нельзя — заказчик просил заводские сайты.
BRANDS = ["XELERON", "HANSMANN", "MAGNUS", "ET", "GMP", "CROSSAIR", "BERG", "ATOM",
          "KRAFTMACHINE", "EXELUTE", "REMEZA", "IRONMAC",
          # ниже — бренды, которых в образце нет, но сайты собраны и заказчик их называл
          "DALI", "COMPRAG", "SOLLANT", "ЗИФ"]
TITLE = {"XELERON": "Xeleron", "HANSMANN": "Hansmann", "MAGNUS": "MAGNUS", "ET": "ET",
         "GMP": "GMP", "CROSSAIR": "Cross-air", "BERG": "BERG", "ATOM": "ATOM",
         "KRAFTMACHINE": "Kraftmachine", "EXELUTE": "Exelute", "REMEZA": "Remeza",
         "IRONMAC": "Ironmac", "DALI": "Dali", "COMPRAG": "Comprag",
         "SOLLANT": "Sollant", "ЗИФ": "ЗИФ"}

# Модели из листа «Сводная» образца — контрольная выборка: на ней сверяем, какой режим
# подстановки умолчаний воспроизводит ручную сцепку заказчика.
PROBA = ["BS-5,5BFR-250", "HC-7,5BFR-250", "HC-7,5DFRE-250", "HC-11BFR-400",
         "HC-11DFRE-400", "HC-15BFR-400", "HC-15DFRE-400", "HC-18,5BFR-450",
         "HC-22BFR-450", "HC-22DFRE-450"]


# --- наша сторона -----------------------------------------------------------------------
def our_ip_by_code() -> dict:
    """IP из выгрузки Битрикса (22959) по имени карточки.

    load_ours_all() это свойство в матчер НЕ пускает — арбитраж 12.08 дал 15 ложных
    отсевов из 18 (PROPS_BITRIX.md). Но в шаблоне заказчика степень защиты — колонка
    ключа, и без неё отчёт не построить. Компромисс: значение показываем и используем
    как тристейт (режем только при явном конфликте обеих сторон), а расхождения выносим
    отдельной пометкой в строке."""
    path = find_ours("specs_compact")
    out = {}
    for r in csv.DictReader(open(path, encoding="utf-8-sig", errors="replace"), delimiter=";"):
        # Ключ — IE_CODE, а не имя. Имя в прайс-фиде и в компакте расходится пробелами и
        # хвостами («…BS-15DFRE-400-5+ 15» против «…-400-5+»), и 65 карточек из-за этого
        # оставались без IP, то есть сцеплялись с любым исполнением.
        code = (r.get("IE_CODE") or "").strip().lower()
        if not code:
            continue
        raw = (r.get("IP_PROP22959") or "").strip() or (r.get("IP_PROP22674") or "").strip()
        cls = ip_classes(raw)
        if cls:
            out[code] = cls
    return out


def our_block_by_code() -> dict:
    """Винтовой блок по коду карточки: сначала выгрузка Битрикса, потом скрейп сайта.

    Порядок такой, потому что выгрузка — первоисточник, а скрейп снимок страницы. После
    пересборки компакта с 22670 (25.08) свойство заполнено у 2 280 винтовых Enger из
    2 701 (84%); скрейп сайта покрывает 2 182 (81%) и закрывает часть оставшихся.

    Значения сырые и разнобойные («Hanbell AB (6 подшипников)», «HANBELL AB», «Baosi»,
    «BAOSI»), в класс шаблона их сводит block_class()."""
    out = {}
    for u, d in load_universe()[1].items():
        if "prokompressor.ru" not in u:
            continue
        v = (d.get("Винтовой блок") or "").strip()
        if v:
            out[u.rstrip("/").split("/")[-1].lower()] = v
    for r in csv.DictReader(open(find_ours("specs_compact"), encoding="utf-8-sig",
                                 errors="replace"), delimiter=";"):
        code = (r.get("IE_CODE") or "").strip().lower()
        v = (r.get("IP_PROP22670") or "").strip() or (r.get("IP_PROP23168") or "").strip()
        if code and v:
            out[code] = v            # выгрузка перекрывает скрейп там, где знают оба
    return out


def our_flags_by_code() -> dict:
    """Явные «да/нет» из Битрикса по ресиверу и осушителю, ключ — IE_CODE.

    load_ours_all() отдаёт только положительные признаки (rv — объём, ff — флаг), а
    «нет» теряет. Для отчёта разница важна: без неё в колонке «На умолчании шаблона»
    честное «нет» выглядит как молчание сайта."""
    out = {}
    for r in csv.DictReader(open(find_ours("specs_compact"), encoding="utf-8-sig",
                                 errors="replace"), delimiter=";"):
        code = (r.get("IE_CODE") or "").strip().lower()
        if not code:
            continue
        d = {}
        for key, col in (("ресивер", "IP_PROP22574"), ("осушитель", "IP_PROP22565")):
            v = (r.get(col) or "").strip().lower()
            if v in ("да", "есть"):
                d[key] = "да"
            elif v == "нет":
                d[key] = "нет"
        if d:
            out[code] = d
    return out


_ENG_TAIL = re.compile(r"\s+(\d+(?:[.,]\d+)?)\s*$")


def enger_family(name: str, bar) -> str:
    """«Винтовой компрессор Enger BS-5,5BFR-250 8» → «BS-5,5BFR-250».

    Хвостовое число отрезаем ТОЛЬКО если оно совпало с давлением карточки: у части серий
    хвост — часть кода («BS-11DFR-400-5»), и слепое отрезание сломало бы имя модели."""
    s = re.sub(r"^.*?\bEnger\b\s*", "", name, flags=re.I).strip()
    m = _ENG_TAIL.search(s)
    if not m:
        return s
    tail = float(m.group(1).replace(",", "."))
    # Хвост срезаем, если он совпал с давлением карточки ИЛИ просто похож на давление
    # (3…40 бар). Второе условие нужно для шести карточек, где имя и Битрикс спорят:
    # «BS-132DT 7» при 8 бар в выгрузке, «HB-18,5BT 16» при 13. Раньше такие имена
    # оставались с хвостом и давали лишнюю строку-двойник в «Сводной».
    if bar and abs(tail - float(bar)) < 0.05:
        return s[:m.start()].strip()
    return s[:m.start()].strip() if 3 <= tail <= 40 else s


def enger_props(o: dict, ip_map: dict, blk_map: dict, flags: dict) -> dict:
    """13 свойств шаблона для нашей карточки. None = «в выгрузке не указано»."""
    p = dict.fromkeys(PROPS)
    nm = o.get("name") or ""
    code = (o.get("url") or "").rstrip("/").split("/")[-1].lower()
    # Явное «нет» из Битрикса — это ДАННЫЕ, а не молчание. Раньше «нет» (1 686 карточек
    # по 22574 и 1 738 по 22565) превращалось в None, и 9 076 пар помечались в отчёте
    # «держится на умолчании», хотя обе стороны честно сказали «нет».
    flag = flags.get(code, {})
    p["ресивер"] = "да" if o.get("rv") else flag.get("ресивер")
    p["осушитель"] = "да" if o.get("ff") == 1 else flag.get("осушитель")
    p["частотник"] = {1: "да", 0: "нет"}.get(o.get("vsd"))
    oil = o.get("oil")
    p["безмасляный"] = {"безмасло": "да", "масл": "нет"}.get(oil)
    # cool_class() отдаёт "air"/"water" (spec_match.py), а не «возд»/«вода» — из-за
    # опечатки в ключах вся колонка охлаждения была пустой, и все пары держались на
    # умолчании «воздушный», включая водяные исполнения LUF…W.
    # cool_class() отдаёт "air"/"water" (spec_match.py), а не «возд»/«вода» — из-за
    # опечатки в ключах колонка была пустой. Но и с исправленным ключом данных нет:
    # свойство 22669 у Enger не заполнено НИ РАЗУ, поэтому основной источник — буква
    # исполнения в коде модели (см. enger_cooling).
    p["охлаждение"] = ({"air": "воздушный", "water": "водяной"}.get(o.get("cool"))
                       or enger_cooling(nm))
    p.update(from_name(nm))          # ступени / передвижной / PM-двигатель / безмасляный из имени
    # Винтовой блок: сначала реальное значение с нашего сайта, и только если его нет —
    # правило заказчика по коду модели. Порядок именно такой: сайт покрывает 81% серии
    # и знает блоки, которых в правиле нет вовсе (JIUYI у LC и OFS, HDHJ у HJ, Ingersoll
    # Rand и ACI у карточек без префикса). Правило остаётся для оставшихся 19%.
    p["блок"] = block_class(blk_map.get(code)) or enger_block(nm)
    # «фильтры» в выгрузке Битрикса нет как свойства (22760 в компакт не выгружается) —
    # по всему нашему каталогу это честное «не указано».
    fuel = "дизель" if re.search(r"дизел|бензин", nm, re.I) else None
    return {"kw": kw_value(o.get("kw")), "bar": bar_bucket(o.get("bar")),
            "ip": ip_map.get(code), "топливо": fuel, **p}


# --- сторона сайтов производителей ------------------------------------------------------
# Хвосты кодов моделей, которые сайт не дублирует в характеристиках. Каждый выведен из
# самих данных, а не из общих соображений (правило 3): рядом на сайте лежат обе версии.
_BERG_RCV = re.compile(r"-(\d{2,4})\s*$")                 # ВК-5.5Р-500 → ресивер 500 л
_BERG_DRY = re.compile(r"[А-Яа-я]*О-\d{2,4}\s*$")         # ВК-5.5РО-500 → «О» = осушитель
_KM_DRY = re.compile(r"/\s*О\b", re.I)                    # КМ5.5-8рВ-500/О → осушитель
_KM_RCV = re.compile(r"-(\d{3,4})(?:/|\s|$)")             # КМ5.5-8рВ-500 → ресивер
_KM_VSD = re.compile(r"\bЧРП\b", re.I)
_HANS_RCV = re.compile(r"-(\d{3,4})\s*(?:DR|DF|D)?\s*$", re.I)   # RSE 7.5-12-500 DR
_HANS_DRY = re.compile(r"\bDR\b|\bDRY\b", re.I)
_IRON_RCV = re.compile(r"(\d{3,4})\s*L\b", re.I)          # IC 7,5/8 DIGI DF 500L
_IRON_DRY = re.compile(r"\bDF\b|\bDRY\b", re.I)
_EX_TAIL = re.compile(r"\bEX([A-Z]*)", re.I)


def vendor_props(row: dict, specs: dict) -> dict:
    """13 свойств шаблона для карточки сайта производителя.

    Сначала явные характеристики сайта (их публикуют GMP, ET, Exelute, Magnus, Dali,
    CrossAir, ЗИФ, Sollant, Comprag), затем — хвосты кода модели там, где сайт свойства
    не печатает (BERG/ATOM печатают только давление, IP и частотник; Kraftmachine —
    только двигатель, IP и мощность; Hansmann и Ironmac ресивер не печатают вовсе)."""
    brand = row["brand"]
    name = row["name"] or ""
    p = dict.fromkeys(PROPS)

    p["блок"] = block_class(pick(specs, r"винтов\w*\s*блок|марка\s+винтов", exclude=r"гарант"))
    p["ресивер"] = yesno(pick(specs, r"^ресивер$|наличие\s+ресивера|на\s+ресивере"))
    if p["ресивер"] is None:
        v = pick(specs, r"об[ъь]?[её]м\s+ресивера")
        if v is not None:
            p["ресивер"] = "нет" if re.search(r"без|^нет$", v, re.I) else ("да" if num(v) else None)
    # «Осушители» во множественном числе — пункт меню сайта, а не свойство: у DALI под
    # этим ключом лежит телефон «+7 (495) …», и _YES ловил его как «да» по ведущему плюсу.
    p["осушитель"] = yesno_or_model(pick(specs, r"осушител(?:ь|ем)\b|с\s+осушителем",
                                         exclude=r"модель\s+осушител|^осушители$"))
    if p["осушитель"] is None:
        p["осушитель"] = yesno_or_model(pick(specs, r"модель\s+осушител"))
    p["частотник"] = yesno_or_model(pick(specs, r"частотн"))
    if p["частотник"] is None and row.get("vsd"):
        p["частотник"] = yesno(row["vsd"])
    p["фильтры"] = yesno_or_model(pick(specs, r"встроенн\w*\s+фильтр|комплектн\w*\s+фильтр"))
    p["безмасляный"] = yesno(pick(specs, r"^безмасляный$"))
    if p["безмасляный"] is None:
        v = pick(specs, r"тип\s+смазки|масляный/безмасляный")
        if v:
            p["безмасляный"] = "да" if re.search(r"безмасл", v, re.I) else "нет"
    p["охлаждение"] = cooling(pick(specs, r"тип\s+охлаждения|система\s+охлаждения|"
                                          r"тип\s+системы\s+охлаждения"))
    p["ступени"] = stages(pick(specs, r"количество\s+ступеней|число\s+ступеней"))
    if p["ступени"] is None:
        v = yesno(pick(specs, r"^двухступенчатый$"))
        p["ступени"] = {"да": "2", "нет": "1"}.get(v)
    p["двигатель"] = motor_class(pick(specs, r"тип\s+(?:электро)?двигател"))
    if p["двигатель"] is None and yesno(pick(specs, r"двигатель\s+на\s+постоянных\s+магнитах")) == "да":
        p["двигатель"] = "синхронный"
    v = pick(specs, r"^передвижной$|^исполнение$")
    if v:
        p["передвижной"] = "да" if re.search(r"да|шасси|прицеп|гусенич", v, re.I) else \
                           ("нет" if re.search(r"нет|стационар", v, re.I) else None)

    for k, val in from_name(name).items():                # маркеры имени поверх молчания сайта
        if p.get(k) is None:
            p[k] = val

    if brand in ("BERG", "ATOM"):
        code = name.split()[-1]
        m = _BERG_RCV.search(code)
        # Хвост «-NNN» у BERG — объём ресивера, но у голых машин там стоит МОЩНОСТЬ:
        # «ВК-11», «ВК-110», «ВК-132». Раньше они все получали «ресивер: да» и сцеплялись
        # с нашими машинами на ресивере (HB-11DF-400 за 472 457 ₽ против голого ВК-11 за
        # 280 273 ₽ — «мы дороже на 69%»). Отличаем по совпадению с кВт карточки.
        vol = num(m.group(1)) if m else None
        if vol is not None and vol == kw_value(row.get("kw")):
            vol = None
        if vol is not None:
            p["ресивер"], p["осушитель"] = "да", ("да" if _BERG_DRY.search(code) else "нет")
        elif p["ресивер"] is None:
            p["ресивер"] = "нет"
    if brand == "KRAFTMACHINE":
        p["осушитель"] = "да" if _KM_DRY.search(name) else (p["осушитель"] or "нет")
        p["ресивер"] = "да" if _KM_RCV.search(name) else (p["ресивер"] or "нет")
        if _KM_VSD.search(name):
            p["частотник"] = "да"
    if brand == "HANSMANN":
        p["ресивер"] = "да" if _HANS_RCV.search(name) else "нет"
        if p["осушитель"] is None:
            p["осушитель"] = "да" if _HANS_DRY.search(name) else None
    if brand == "IRONMAC":
        p["ресивер"] = "да" if _IRON_RCV.search(name) else (p["ресивер"] or "нет")
        if p["осушитель"] is None and _IRON_DRY.search(name):
            p["осушитель"] = "да"
    if brand == "XELERON" and "bezmaslyanye" in (row.get("url") or ""):
        p["безмасляный"] = "да"      # раздел сайта — единственный признак: в имени его нет
    if brand == "XELERON":
        # Раздел «vintovye-kompressory-dry-tank» и приставка «Dry T250» в имени — это и есть
        # комплектация «ресивер 250 л + осушитель»: у Xeleron она обозначена только так,
        # характеристик ресивера в таблице нет.
        dry = re.search(r"\bdry\s*t\s*\d+", name, re.I) or "dry-tank" in (row.get("url") or "")
        p["ресивер"] = p["осушитель"] = "да" if dry else "нет"
    # Суффикс «на постоянных магнитах» пишут слитно с кодом — Xeleron «Z10PMA»,
    # Kraftmachine «КМ11-8ПМ». \bPM\b на них не срабатывает, поэтому отдельным правилом.
    if p["двигатель"] is None and re.search(r"\d\s*(?:PMA?|ПМ)\b", name):
        p["двигатель"] = "синхронный"

    # IP на трёх сайтах записан в ИМЯ ключа, а не в значение: у crossair ключ
    # «Степень защиты IP 23, IP 55» несёт значение «Класс изоляции F» (84 карточки из
    # 114), у dali «Класс защиты IP 54, класс изоляции F.» (70 из 320), у ironmac класс
    # лежит в ключе «Тип двигателя» со значением «IP65» (61 из 101). Без разбора ключей
    # эти карточки шли без IP и сцеплялись с чужим исполнением.
    # Род привода в 13 колонок шаблона не входит (там «асинхронный/синхронный», то есть
    # про обмотку), но сцеплять дизельную передвижную станцию с нашим электрическим
    # компрессором бессмысленно: BS-132DT за 1 433 805 ₽ против Cross Air Borey 180-10B
    # за 2 274 650 ₽. Держим отдельным служебным полем.
    drive = pick(specs, r"^тип\s+двигател") or ""
    fuel = "дизель" if re.search(r"дизел|бензин", drive + " " + name, re.I) else None

    ip = (ip_classes(row.get("ip") or "")
          or ip_classes(pick(specs, r"степень\s+защиты|класс\s+защиты|уровень\s+защиты") or "")
          or ip_classes(" ".join(k for k in specs if re.search(r"IP\s*\d{2}", str(k)))))
    return {"kw": kw_value(row.get("kw")), "bar": bar_bucket(row.get("bar")),
            "ip": ip, "топливо": fuel, **p}


_SCREW = re.compile(r"винтов|vintov|screw", re.I)
# Не компрессор вовсе. Слово должно стоять В НАЧАЛЕ названия — это заголовок товара.
# Без привязки к началу regex ловил «маслоЗАПОЛНЕННЫЙ» и выбрасывал 1 262 карточки
# Kraftmachine («Винтовой электрический маслозаполненный компрессор BS15-16ПМ»).
_NOT_COMPR = re.compile(r"^\s*(?:комплектн\w+\s+|рефрижераторн\w+\s+|адсорбционн\w+\s+)?"
                        r"(?:осушител|ресивер|воздухосборник|фильтр|масло|сепаратор|"
                        r"генератор\s+азота|конденсатоотводчик|запчаст|воздуходувк|"
                        r"труб|бустер|дожимн|винтов\w*\s+блок)", re.I)
# Явно другая форма машины. Отсеиваем ТОЛЬКО по явному признаку: у Kraftmachine и Sollant
# винтовые двухступенчатые называются «Двухступенчатый компрессор SLTT-22V» — слова
# «винтовой» нет ни в имени, ни в адресе, и требование этого слова теряло 591 карточку.
# Форму машины ищем и в АДРЕСЕ: Magnus называет поршневые «Компрессор воздушный
# MAGNUS KW-750/250», слова «поршневой» в имени нет вовсе, и 35 таких карточек уходили
# в аналоги винтовым — BS-5,5BF-250 за 229 245 ₽ «сравнивался» с KW-750/250 за 74 432 ₽.
_OTHER_FORM = re.compile(r"спиральн|scroll|поршнев|piston|центробежн|мембранн|"
                         r"порш\w*\s+компрессор|/porshnev|/spiraln|/scroll|/piston", re.I)
_NAME_BAR = re.compile(r"\d+(?:[.,]\d+)?\s*/\s*(\d{1,2})(?:\D|$)")


def load_vendors() -> list:
    """Винтовые компрессоры со всех собранных сайтов производителей."""
    out = []
    for path in sorted(VENDORS.glob("*.csv")):
        if path.name.endswith("_sverka.csv"):
            continue
        for row in csv.DictReader(open(path, encoding="utf-8-sig"), delimiter=";"):
            name = row.get("name") or ""
            place = name + " " + (row.get("url") or "")
            # «Винтовой блок Hanbell AC 130» у Sollant и «Осушитель RDX150» у Comprag лежат
            # в тех же разделах, что и компрессоры: режем по имени, а не по разделу.
            if _NOT_COMPR.search(name) or _OTHER_FORM.search(place):
                continue
            if not _SCREW.search(place) and not re.search(r"kompressor|компрессор", place, re.I):
                continue
            if not (row.get("bar") or "").strip():
                # Exelute на 136 карточках не выводит давление в таблицу, но пишет его в
                # названии вторым числом («EXT PM 22/12 IP54» = 22 кВт / 12 бар).
                m = _NAME_BAR.search(name)
                if m:
                    row = dict(row, bar=m.group(1))
            try:
                specs = json.loads(row.get("specs") or "{}")
            except Exception:
                specs = {}
            if not isinstance(specs, dict):
                specs = {}
            p = vendor_props(row, specs)
            if p["kw"] is None or p["bar"] is None:
                continue
            # У GMP на части карточек высокого давления диапазон склеен в одно число:
            # «5080» вместо 50–80 бар, «150250», «330400». Порог 400, а не 60: 80, 100 и
            # 350 бар — это реальные дыхательные и дожимные машины GMP HB, их резать
            # нельзя (проверено по именам: «GMP HB 22-80», «GMP HB 30-100»).
            if p["bar"].isdigit() and int(p["bar"]) > 400:
                continue
            try:
                price = float(row["price"]) if row.get("price") else None
            except ValueError:
                price = None
            out.append(dict(brand=row["brand"], name=name, url=row.get("url"), price=price,
                            file=path.stem, props=p))
    # Дедуп. Одна карточка часто попадает в выгрузку несколько раз: у Kraftmachine
    # «КМ110-10 ВБМ» лежит и в разделе по мощности, и в разделе по давлению, и в одном из
    # них цены нет. Ключ включает весь вектор свойств, поэтому исполнения по IP (у KM это
    # реально разные SKU: IP54 и IP65) остаются раздельными, а полные дубли схлопываются
    # в карточку с ценой — иначе «аналогов 7» на деле означало бы три разных машины.
    best = {}
    for c in out:
        k = (c["brand"], c["name"], tuple(sorted((str(a), str(b)) for a, b in c["props"].items())))
        cur = best.get(k)
        if cur is None or (c["price"] and (not cur["price"] or c["price"] < cur["price"])):
            best[k] = c
    return list(best.values())


# --- сцепка -----------------------------------------------------------------------------
def value(p: dict, key: str, strict: bool):
    """Значение свойства с учётом умолчания.

    strict=True — буква правила заказчика: молчание сайта = первое значение столбца.
    strict=False — умолчание подставляется только когда молчат ОБЕ стороны, то есть
    молчание не режет кандидата (правило 4 «молчание = совместимо»)."""
    v = p.get(key)
    if v is None and strict:
        return DEFAULTS[key]
    return v


# Винтовой блок — тристейт, как IP: сравниваем, только когда известен с ОБЕИХ сторон.
#
# Умолчание шаблона здесь ломает больше, чем чинит, и это видно на числах. Пока блока не
# было ни у нас, ни у конкурентов, обе стороны получали «Baosi» и всегда сходились. Как
# только с нашей стороны появились настоящие значения (1 496 карточек), выяснилось, что у
# конкурентов блок не указан на 5 635 карточках из 7 982 — они уезжают в то же умолчание,
# и наш Hanbell AC перестаёт совпадать с их безымянным блоком. Строгий режим на этом
# терял 8 253 пары из 12 148, включая ручные сцепки самого заказчика из образца
# (HC-7,5BFR-250 ↔ Cross-air CA7.5-8RA-500DRY: у нас Hanbell AC, сайт Cross-air блок не
# печатает). В шаблоне рядом с этой колонкой и стоит пометка «часто не указывается».
TRISTATE = {"блок"}


def match(o: dict, c: dict, strict: bool) -> tuple[bool, list]:
    """Совпал ли кандидат. Возвращает (да/нет, список свойств, которые держатся на умолчании)."""
    if o["kw"] != c["kw"] or o["bar"] != c["bar"]:
        return False, []
    if o["ip"] and c["ip"] and not (o["ip"] & c["ip"]):     # IP — тристейт, см. enger_specs
        return False, []
    if o.get("топливо") != c.get("топливо"):               # дизель ↔ электричество не пара
        return False, []
    weak = []
    for k in TRISTATE:
        a, b = o.get(k), c.get(k)
        if a and b and a != b:
            return False, []
        if a is None or b is None:
            weak.append(k)
    for k in PROPS:
        if k in TRISTATE:
            continue
        a, b = value(o, k, strict), value(c, k, strict)
        # Считаем «слабым» свойство, по которому молчит РОВНО ОДНА сторона: именно оно и
        # есть причина, по которой строгий режим пару отбросил. Раньше сюда попадало
        # двойное молчание — то есть колонка «Молчат свойства» на листе «Кандидаты»
        # перечисляла что угодно, кроме причины.
        one_side = (o.get(k) is None) != (c.get(k) is None)
        if a is None or b is None:                          # мягкий режим: молчит хоть одна
            if one_side:
                weak.append(k)
            continue
        if a != b:
            return False, []
        if one_side:
            weak.append(k)
    return True, weak


def model_label(name: str) -> str:
    """Код модели для ячейки: без слов-категорий, но С сохранением IP — заказчик в образце
    пишет «CA5.5-8RA-300DRY (IP54)», для него исполнение по защите часть модели."""
    s = re.sub(r"(?i)^(винтовой|двухступенчатый|безмасляный|дизельный|электрический|"
               r"маслозаполненный|мобильный|передвижной|компрессор|воздушный|на\s+постоянных\s+"
               r"магнитах|серии|с\s+прямым\s+приводом|с\s+ременным\s+приводом|"
               r"с\s+частотным\s+преобразователем|на\s+ресивере|со\s+встроенным\s+осушителем|"
               r"[,\s])+", " ", name)
    s = re.sub(r"\s+\d{8,}\s*$", "", s)                     # артикул Comprag в хвосте имени
    s = re.sub(r"\s+", " ", s).strip(" ,-·")
    # Comprag пишет комплектацию прозой: «серии F с ременным приводом на ресивере FR-0510-270».
    # Прозу в ячейку тащить незачем — код модели стоит последним и однозначно узнаётся.
    if len(s) > 34:
        m = re.search(r"([A-ZА-Я]{1,5}-?\d[\w./,-]*)\s*$", s)
        if m:
            s = m.group(1)
    return s or name


# Свойства комплектации: ими отличается «машина на ресивере с осушителем» от голой машины.
# Четыре завода (ATOM, Kraftmachine кроме КМ11, Ironmac, Magnus частично) такие исполнения
# на сайте не публикуют вовсе — по строгому ключу у них аналога нет и быть не может.
# Чтобы это не выглядело как «аналога нет в природе», собираем их отдельным листом.
KOMPLEKT = {"ресивер", "осушитель", "фильтры"}


def build(proba=False, strict=True):
    ip_map = our_ip_by_code()
    blk_map = our_block_by_code()
    flags = our_flags_by_code()
    ours = [o for o in load_ours_all().get("enger", [])
            if _SCREW.search(o.get("name") or "") and o.get("kw") and o.get("bar")]
    if proba:
        ours = [o for o in ours if any(m in o["name"] for m in PROBA)]
    cands = load_vendors()

    by_kw = defaultdict(list)
    for c in cands:
        by_kw[c["props"]["kw"]].append(c)

    rows = []
    for o in ours:
        p = enger_props(o, ip_map, blk_map, flags)
        hits, soft, base, ladder = (defaultdict(list), defaultdict(list),
                                    defaultdict(list), defaultdict(list))
        ocls = bar_classes(p["bar"])
        for c in by_kw.get(p["kw"], []):
            ok, weak = match(p, c["props"], strict)
            if ok:
                hits[c["brand"]].append((c, weak))
                continue
            # мягкий разбор нужен только там, где строгий ничего не дал — иначе шум
            ok2, weak2 = match(p, c["props"], False)
            if ok2:
                soft[c["brand"]].append((c, weak2))
                continue
            ok3, diff = match_base(p, c["props"])
            if ok3:
                base[c["brand"]].append((c, diff))
                continue
            # последний заход: давление приведено к ряду шаблона с обеих сторон
            if ocls & bar_classes(c["props"]["bar"]) and p["bar"] != c["props"]["bar"]:
                ok4, _ = match(dict(p, bar="~"), dict(c["props"], bar="~"), strict)
                if ok4:
                    ladder[c["brand"]].append((c, sorted(ocls & bar_classes(c["props"]["bar"]))))
        rows.append(dict(o=o, props=p, family=enger_family(o["name"], o.get("bar")),
                         block_raw=blk_map.get((o.get("url") or "").rstrip("/").split("/")[-1].lower()),
                         hits=hits, soft=soft, base=base, ladder=ladder))
    return rows, cands


def match_base(o: dict, c: dict) -> tuple[bool, list]:
    """«Та же машина, другая комплектация»: сходится всё, кроме ресивера/осушителя/фильтров."""
    if o["kw"] != c["kw"] or o["bar"] != c["bar"]:
        return False, []
    if o["ip"] and c["ip"] and not (o["ip"] & c["ip"]):
        return False, []
    diff = []
    for k in PROPS:
        if k in TRISTATE:                       # блок — только при явном конфликте, см. match()
            a, b = o.get(k), c.get(k)
            if a and b and a != b:
                return False, []
            continue
        a, b = value(o, k, True), value(c, k, True)
        if a == b:
            continue
        if k not in KOMPLEKT:
            return False, []
        diff.append(f"{k}: у нас {a}, у них {b}")
    return bool(diff), diff


_REAL_KEY = re.compile(r"^([A-ZА-Я]{2}-\d+(?:[.,]\d+)?)([BD])([TFS])R?[СC]?(E?)")


def real_flow_table() -> dict:
    """Лист «реальная пр-ть» из образца заказчика: модель → (8 бар, 10 бар), м³/мин.

    Таблица ручная и живёт у заказчика: это его замеры против паспортных цифр. Держим её
    в data/ рядом с проектом, а не читаем из папки загрузок — та живёт одну сессию."""
    out = {}
    path = _HERE / "data" / "enger_realnaya_prt.csv"
    if not path.exists():
        return out
    for r in csv.DictReader(open(path, encoding="utf-8-sig"), delimiter=";"):
        out[(r["модель"] or "").strip()] = (num(r.get("8 бар")), num(r.get("10 бар")))
    return out


def real_key(family: str) -> str:
    """«HC-7,5DFRE-250» → «HC-7,5DFE»: в таблицах заказчика модель записана без ресивера
    («R») и без объёма, но с буквой частотника («E»)."""
    m = _REAL_KEY.match(family)
    return "".join(m.groups()) if m else ""


def report(rows, tag):
    """Числа до/после — без них изменение не считается понятым (правило 2)."""
    n = len(rows)
    with_any = sum(1 for r in rows if r["hits"])
    pairs = sum(len(v) for r in rows for v in r["hits"].values())
    brands = sum(len(r["hits"]) for r in rows)
    print(f"  {tag:<22} строк {n:>5} | с аналогами {with_any:>5} ({with_any/max(n,1):.0%}) | "
          f"пар {pairs:>6} | бренд-совпадений {brands:>5}")
    return with_any, pairs


# --- запись отчёта ----------------------------------------------------------------------
HFILL = PatternFill("solid", fgColor="305496")
HFONT = Font(bold=True, color="FFFFFF")
CTR = Alignment(horizontal="center", vertical="center", wrap_text=True)
LINK = Font(color="0563C1", underline="single")
RED = PatternFill("solid", fgColor="FFFFC7CE")       # конкурент дешевле нас
GREY = Font(color="808080", italic=True)           # значение держится на умолчании
FIX = ["Модель Enger", "Бар", "Пр-ть, м³/мин", "Реальная\nпр-ть", "Наша цена, ₽",
       "Цена мин", "Цена сред", "Цена new", "% сниж", "Мы дороже на", "Аналогов"]


def head(ws, titles, row=1):
    for i, t in enumerate(titles, 1):
        c = ws.cell(row, i, t)
        c.font, c.fill, c.alignment = HFONT, HFILL, CTR


def cheapest(lst):
    """Самый дешёвый аналог бренда; карточки без цены — в конец (цена нужна для сравнения)."""
    return min(lst, key=lambda t: (t[0]["price"] is None, t[0]["price"] or 0))


# Заливки эталона. Держим их темой книги, а не готовым RGB: у заказчика в файле стоят
# именно theme+tint, и при смене темы Office отчёт должен перекраситься так же, как его
# собственный. theme 7 = accent4 (охра), 8 = accent5 (голубой), 0 = фон.
F_MODEL = PatternFill("solid", start_color=Color(theme=7, tint=0.8))    # колонка модели
F_PRICE = PatternFill("solid", start_color=Color(theme=8, tint=0.6))    # наша цена + шапка
F_CALC = PatternFill("solid", start_color=Color(theme=7, tint=0.8))     # блок расчёта I..N
F_NEW = PatternFill("solid", start_color=Color(theme=9, tint=0.6))      # колонки «new»
F_EMPTY = PatternFill("solid", start_color=Color(theme=0, tint=-0.35))  # аналога нет
F_WHITE = PatternFill("solid", start_color=Color(theme=0, tint=0.0))


def dxf_fill(rgb: str) -> PatternFill:
    """Заливка для правила условного форматирования.

    В обычной ячейке solid-заливка берётся из fgColor, а в dxf (differential formatting,
    то есть в правилах) Excel красит по bgColor. openpyxl из PatternFill("solid",
    start_color=X) пишет только fgColor — правило срабатывало, но фон оставался белым, и
    в отчёте красился один шрифт. У заказчика в образце заданы оба цвета, делаем так же."""
    return PatternFill(fill_type="solid", start_color=rgb, end_color=rgb)

RUB = r'_-* #,##0\ _₽_-;\-* #,##0\ _₽_-;_-* "-"\ _₽_-;_-@_-'
ACC = r'_-* #,##0.00_-;\-* #,##0.00_-;_-* "-"??_-;_-@_-'   # «накрутка», формат образца
HDR_FONT = Font(bold=True, size=12)
HDR_AL = Alignment(horizontal="center", vertical="center", wrap_text=True)

# Порядок предпочтения давления при выборе строки-представителя модели. Эталон сцепляет
# по 8 бар; если у серии его нет (LUF идёт на 7,5 / 8,6 / 10,4), спускаемся по ряду.
BAR_PREF = ["8", "10", "7", "12", "13", "15/16", "20", "25", "30", "40"]


def svodnaya_etalon(wb, rows, real):
    """Лист «Сводная» в раскладке и цветах присланного образца.

    Строка = МОДЕЛЬ Enger (как у заказчика), на модель отводится две строки — во второй
    идёт следующий по цене аналог бренда; ровно так сделано в образце, где у Cross-air
    в первой строке стоит исполнение IP54, во второй IP23. Колонки A/B/C/H заполняются
    только в первой строке группы, и формулы «разницы» второй строки ссылаются на её H."""
    ws = wb.create_sheet("Сводная", 0)
    # Заголовки посимвольно как в образце, включая хвостовые пробелы в «Макс\n пр-ть »
    # и «Наша Цена » и ведущий в « цена new» — заказчик сверяет шапку глазами.
    fix = ["Модель Enger", "8 бар", "10 бар", "Макс\n пр-ть ", "Реальная\nпр-ть", "пр-ть new",
           "% к 10 реальной", "Наша Цена ", "цена мин", "цена сред", " цена new", "% сниж",
           "мы дороже на", "накрутка после изменения цены"]
    n_fix = len(fix)
    b0 = n_fix + 1                       # короткий блок «цена по брендам» (O–Z образца)
    t0 = b0 + len(BRANDS)                # тройки «Бренд | Цена | разница» (с AA образца)
    for i, t in enumerate(fix + [TITLE[b] for b in BRANDS] +
                          [x for b in BRANDS for x in (TITLE[b], "Цена", "разница")], 1):
        c = ws.cell(1, i, t)
        c.font, c.alignment = HDR_FONT, HDR_AL
        # В образце голубым залито только имя бренда и «Наша Цена»; «Цена» в тройке белая,
        # «разница» — вообще без заливки. Повторяем ровно так.
        if i in (1, 8) or b0 <= i < t0 or (i >= t0 and (i - t0) % 3 == 0):
            c.fill = F_PRICE
        elif i in (6, 11, 13, 14):       # «пр-ть new», «цена new», «мы дороже», «накрутка»
            c.fill = F_NEW
        elif i >= t0 and (i - t0) % 3 == 2:
            pass
        else:
            c.fill = F_WHITE
    ws.row_dimensions[1].height = 128.25

    groups = defaultdict(dict)
    for r in rows:
        groups[r["family"]].setdefault(r["props"]["bar"], r)
    pcols = [get_column_letter(t0 + 3 * j + 1) for j in range(len(BRANDS))]

    # Порядок строк — по мощности, как у заказчика (5,5 → 7,5 → 11 → 15 → 18,5 → 22).
    # Алфавитный ставил HC-7,5 после HC-22, и ряд переставал читаться.
    def by_kw(fam):
        b = groups[fam]
        kw = next((r["props"]["kw"] for r in b.values() if r["props"]["kw"]), 0)
        return (kw, fam)

    for fam in sorted(groups, key=by_kw):
        bars = groups[fam]
        pick_bar = next((b for b in BAR_PREF if b in bars), next(iter(bars)))
        r0 = bars[pick_bar]
        top = ws.max_row + 1
        # сколько строк на модель: как в образце — две, но если у бренда всего один аналог,
        # вторая строка просто останется серой
        depth = min(2, max([len(v) for v in r0["hits"].values()] or [1]))
        for k in range(depth):
            i = top + k
            if k == 0:
                ws.cell(i, 1, fam)
                if r0["o"].get("url"):
                    ws.cell(i, 1).hyperlink = r0["o"]["url"]
                    ws.cell(i, 1).font = Font(color="0563C1", underline="single")
                b8, b10 = bars.get("8"), bars.get("10")
                ws.cell(i, 2, round(b8["o"]["fl"] / 1000, 2) if b8 and b8["o"].get("fl") else None)
                ws.cell(i, 3, round(b10["o"]["fl"] / 1000, 2) if b10 and b10["o"].get("fl") else None)
                rk = real.get(real_key(fam), (None, None))
                ws.cell(i, 5, rk[1] if pick_bar == "10" else rk[0])
                ws.cell(i, 8, r0["o"].get("price"))
                rng = ",".join(f"{c}{i}" for c in pcols)
                # MIN по пустому диапазону даёт 0, а не ошибку — поэтому COUNT, а не IFERROR:
                # без этого «цена мин» показывала 0 ₽ у всех моделей без аналогов
                ws.cell(i, 9, f'=IF(COUNT({rng})=0,"",MIN({rng}))')
                ws.cell(i, 10, f'=IF(COUNT({rng})=0,"",AVERAGE({rng}))')
                # Обе колонки молчат, когда нашей цены нет. Без проверки H пустая ячейка
                # читается как 0, и «мы дороже на» показывало −100% у 95 моделей, которых
                # попросту нет в прайсе (BS-7,5BFR-250 — 0 строк в products_export).
                ws.cell(i, 12, f'=IF(OR(N(K{i})=0,H{i}=""),"",IFERROR((K{i}-H{i})/H{i},""))')
                ws.cell(i, 13, f'=IF(H{i}="","",IFERROR((H{i}-I{i})/I{i},""))')
                # Накрутка считается как в образце: цена / закупку. Две поправки к его
                # формуле, обе вынужденные. Первая: ключом идёт не A2, а код без ресивера
                # («BS-5,5BFR-250» → «BS-5,5BF») — в таблице закупок модели записаны так,
                # и VLOOKUP по полному имени возвращал «*» на каждой строке. Вторая: закуп
                # в долларах, а наша цена в рублях, поэтому делим ещё на курс — он лежит
                # одной ячейкой накрутки!$F$1, чтобы его можно было поправить руками.
                rk = real_key(fam) or fam
                buy = f'VLOOKUP("{rk}",накрутки!A:C,2,FALSE)*накрутки!$F$1'
                ws.cell(i, 14, f'=IFERROR(IF(N(K{i})=0,H{i}/({buy}),K{i}/({buy})),"*")')
            for col in (1,):
                ws.cell(i, col).fill = F_MODEL
            for col in (2, 3, 5):                 # B, C, E — белые, как в образце
                ws.cell(i, col).fill = F_WHITE
            for col in (4, 6, 7):                 # D, F, G — охра, как в образце
                ws.cell(i, col).fill = F_CALC
            ws.cell(i, 7).number_format = "0%"    # «% к 10 реальной»
            ws.cell(i, 8).fill = F_PRICE
            for col in range(9, 15):
                ws.cell(i, col).fill = F_CALC
                ws.cell(i, col).number_format = RUB if col in (9, 10, 11) else (
                    "0%" if col in (12, 13) else ACC)
            for j, b in enumerate(BRANDS):
                mc, pc, dc = t0 + 3 * j, t0 + 3 * j + 1, t0 + 3 * j + 2
                lst = sorted(r0["hits"].get(b, []),
                             key=lambda t: (t[0]["price"] is None, t[0]["price"] or 0))
                # Короткий блок пишем в ОБЕИХ строках группы: в образце он тоже есть в
                # обеих (O5=AB5, T3=AN3), иначе второе исполнение выпадает из градиента.
                ws.cell(i, b0 + j, f'=IF({get_column_letter(pc)}{i}="","",'
                                   f'{get_column_letter(pc)}{i})')
                ws.cell(i, b0 + j).number_format = RUB
                if k >= len(lst):
                    for col in (mc, pc, dc):
                        ws.cell(i, col).fill = F_EMPTY      # серым — аналога нет, как в образце
                    continue
                c, weak = lst[k]
                cell = ws.cell(i, mc, model_label(c["name"]))
                if c.get("url"):
                    cell.hyperlink = c["url"]
                    cell.font = Font(color="0563C1", underline="single",
                                     italic=bool(weak))
                elif weak:
                    cell.font = Font(italic=True, color="808080")
                ws.cell(i, pc, c["price"]).number_format = RUB
                ws.cell(i, dc, f'=IFERROR((H{top}-{get_column_letter(pc)}{i})/'
                               f'{get_column_letter(pc)}{i},"")').number_format = "0%"
    # Градиент по короткому блоку брендов — ступенями, а не цветовой шкалой.
    #
    # Пробовали два способа шкалы, оба отвергнуты на данных:
    #   * шкала образца (min/percentile/max по строке) не зависит от нашей цены и при
    #     единственном аналоге красит его зелёным всегда — таких строк 225 из 663;
    #   * шкала с порогами-формулами от $H (cfvo type="formula") Excel молча выбрасывает
    #     вместе со всем условным форматированием листа — в открытом файле цветов не
    #     осталось вовсе.
    # Пять обычных правил по формуле работают везде и дают тот же визуальный градиент.
    # Границы сняты с данных: по 2 061 паре с ценами отношение «их цена / наша» имеет
    # квантили 0,55 (5%), 0,78 (25%), 0,97 (медиана), 1,21 (75%), 1,58 (90%).
    # Оттенки — линейная растяжка между цветами образца F8696B → FFEB84 → 63BE7B.
    last = ws.max_row
    first = get_column_letter(b0)
    band = f"{first}2:{get_column_letter(b0 + len(BRANDS) - 1)}{last}"
    STEPS = [("<0.7", "FFF8696B"), (">=0.7,<0.9", "FFFBAA77"), (">=0.9,<1.1", "FFFFEB84"),
             (">=1.1,<1.4", "FFB1D47F"), (">=1.4", "FF63BE7B")]
    for cond, rgb in STEPS:
        parts = [f'{first}2<>""', '$H2<>""']
        for c in cond.split(","):
            parts.append(f"{first}2/$H2{c}")
        ws.conditional_formatting.add(band, FormulaRule(
            formula=[f'AND({",".join(parts)})'], fill=dxf_fill(rgb)))

    # Колонки «разница». Разница = (наша цена − цена конкурента) / цена конкурента,
    # поэтому ПЛЮС значит «мы дороже» — и красится он КРАСНЫМ, минус зелёным.
    #
    # Прошлый вывод «плюс = зелёный, 682 ячейки против 338» был получен подсчётом всех
    # 714 правил образца подряд и оказался неверным: Excel применяет то правило, у
    # которого меньше `priority`, а у заказчика поверх старых правил лежат новые с
    # обратной полярностью. Пересчёт по приоритету даёт 129 ячеек «плюс = красный»
    # против 68. Пример стека на AX2: (1157, >0, красный), (1158, <0, зелёный),
    # (1345, <0, красный), (1346, >0, зелёный) — побеждают 1157 и 1158.
    # Сходится и со смыслом: у заказчика в «Выводах» строка «Мы дороже Exelute и ET на
    # 12-25%» стоит как проблема, а не как достижение.
    for j in range(len(BRANDS)):
        d = get_column_letter(t0 + 3 * j + 2)
        for rule in (CellIsRule(operator="greaterThan", formula=["0"],
                                fill=dxf_fill("FFFFC7CE"), font=Font(color="FF9C0006")),
                     CellIsRule(operator="lessThan", formula=["0"],
                                fill=dxf_fill("FFC6EFCE"), font=Font(color="FF006100"))):
            ws.conditional_formatting.add(f"{d}2:{d}{last}", rule)
    for rule in (CellIsRule(operator="greaterThan", formula=["0"],
                            fill=dxf_fill("FFFFC7CE"), font=Font(color="FF9C0006")),
                 CellIsRule(operator="lessThan", formula=["0"],
                            fill=dxf_fill("FFC6EFCE"), font=Font(color="FF006100"))):
        ws.conditional_formatting.add(f"G2:G{last}", rule)   # «% к 10 реальной», как в образце

    ws.freeze_panes = "B2"
    ws.column_dimensions["A"].width = 27.9
    for col, w in (("B", 9.9), ("C", 8.3), ("D", 1.0), ("E", 14.4), ("F", 8.4), ("G", 13.3),
                   ("H", 12.0), ("I", 16.4), ("J", 11.4), ("K", 11.4), ("L", 1.0),
                   ("M", 11.9), ("N", 13.0)):
        ws.column_dimensions[col].width = w
    for j in range(len(BRANDS)):
        ws.column_dimensions[get_column_letter(b0 + j)].width = 11.9
        ws.column_dimensions[get_column_letter(t0 + 3 * j)].width = 26
        ws.column_dimensions[get_column_letter(t0 + 3 * j + 1)].width = 12.3
        ws.column_dimensions[get_column_letter(t0 + 3 * j + 2)].width = 9.9
    return ws


def copy_table(wb, title, path, widths=(26, 14, 14)):
    """Таблицы заказчика («накрутки», «реальная пр-ть») кладём в книгу как отдельные листы —
    иначе VLOOKUP в колонке «накрутка» ссылается в пустоту."""
    ws = wb.create_sheet(title)
    if not Path(path).exists():
        return ws
    for r in csv.reader(open(path, encoding="utf-8-sig"), delimiter=";"):
        ws.append([num(v) if i and v not in ("", "*") else v for i, v in enumerate(r)])
    for i in range(1, 4):
        c = ws.cell(1, i)
        c.font, c.fill, c.alignment = HDR_FONT, F_PRICE, HDR_AL
        ws.column_dimensions[get_column_letter(i)].width = widths[i - 1]
    if title == "накрутки":
        # Курс отдельной ячейкой, а не константой в формуле: закуп в таблице заказчика
        # в долларах, наши цены в рублях, и курс он правит чаще, чем всё остальное.
        ws["E1"], ws["F1"] = "Курс ₽/$", 78.9
        ws["E1"].font = HDR_FONT
        ws.column_dimensions["E"].width = 12
    ws.freeze_panes = "A2"
    return ws


def save(rows, cands, out=OUT):
    real = real_flow_table()
    wb = openpyxl.Workbook()

    # --- ЛИСТ «Сводная по барам» — строка на каждое давление --------------------------
    ws = wb.active
    ws.title = "Сводная по барам"
    n_fix = len(FIX)
    b0 = n_fix + 1                       # блок «цена по брендам» (как колонки O–Z образца)
    t0 = b0 + len(BRANDS)                # тройки «Модель | Цена | разница»
    head(ws, FIX + [TITLE[b] for b in BRANDS] +
             [x for b in BRANDS for x in (TITLE[b], "Цена", "разница")])
    for r in rows:
        o, p = r["o"], r["props"]
        i = ws.max_row + 1
        rk = real.get(real_key(r["family"]), (None, None))
        rf = rk[0] if p["bar"] == "8" else (rk[1] if p["bar"] == "10" else None)
        ws.cell(i, 1, r["family"])
        if o.get("url"):
            ws.cell(i, 1).hyperlink = o["url"]
            ws.cell(i, 1).font = LINK
        ws.cell(i, 2, num(p["bar"]) if p["bar"] != "15/16" else "15/16")
        ws.cell(i, 3, round(o["fl"] / 1000, 3) if o.get("fl") else None)
        ws.cell(i, 4, rf)
        ws.cell(i, 5, o.get("price"))
        cols = [get_column_letter(t0 + 3 * j + 1) for j in range(len(BRANDS))]
        rng = ",".join(f"{c}{i}" for c in cols)
        # MIN по пустому диапазону даёт 0, а не ошибку, поэтому IFERROR тут не спасает —
        # ровно тот же баг, что был на «Сводной». 706 строк из 2 528 не имеют ни одного
        # аналога, и без COUNT они показывали «цена мин» = 0 ₽.
        ws.cell(i, 6, f'=IF(COUNT({rng})=0,"",MIN({rng}))')
        ws.cell(i, 7, f'=IF(COUNT({rng})=0,"",AVERAGE({rng}))')
        ws.cell(i, 9, f'=IF(OR(N(H{i})=0,E{i}=""),"",IFERROR((H{i}-E{i})/E{i},""))')
        ws.cell(i, 10, f'=IF(E{i}="","",IFERROR((E{i}-F{i})/F{i},""))')
        ws.cell(i, 11, sum(len(v) for v in r["hits"].values()))
        for j, b in enumerate(BRANDS):
            lst = r["hits"].get(b)
            mc, pc, dc = t0 + 3 * j, t0 + 3 * j + 1, t0 + 3 * j + 2
            ws.cell(i, b0 + j, f'=IF({get_column_letter(pc)}{i}="","",'
                               f'{get_column_letter(pc)}{i})')
            if not lst:
                continue
            c, weak = cheapest(lst)
            label = model_label(c["name"])
            if len(lst) > 1:
                label += f"  (+{len(lst)-1})"          # остальные — на листе «Все совпадения»
            cell = ws.cell(i, mc, label)
            if c.get("url"):
                cell.hyperlink, cell.font = c["url"], LINK
            if weak:
                cell.font = GREY                        # сцепка держится на умолчании шаблона
            ws.cell(i, pc, c["price"])
            ws.cell(i, dc, f'=IFERROR((E{i}-{get_column_letter(pc)}{i})/'
                           f'{get_column_letter(pc)}{i},"")')
            if c["price"] and o.get("price") and c["price"] < o["price"]:
                ws.cell(i, pc).fill = RED
    for i in range(2, ws.max_row + 1):
        for col in [5, 6, 7, 8] + [t0 + 3 * j + 1 for j in range(len(BRANDS))] + \
                   [b0 + j for j in range(len(BRANDS))]:
            ws.cell(i, col).number_format = "# ##0"
        for col in [9, 10] + [t0 + 3 * j + 2 for j in range(len(BRANDS))]:
            ws.cell(i, col).number_format = "0%"
    ws.freeze_panes = "B2"
    ws.column_dimensions["A"].width = 26
    for j in range(len(BRANDS)):
        ws.column_dimensions[get_column_letter(t0 + 3 * j)].width = 30
    ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"

    # --- ЛИСТ «Свойства Enger» — из чего сложился ключ --------------------------------
    ws2 = wb.create_sheet("Свойства Enger")
    # Две служебные колонки в конце. «Блок на сайте (как есть)» — сырое значение с нашей
    # карточки до сведения в класс шаблона: без него не видно, что у 589 карточек блок
    # ИЗВЕСТЕН, но строки под него в шаблоне нет вовсе (Ingersoll Rand, Jiuyi, ACI, HDHJ,
    # Hanbell AA). «Откуда блок» отделяет данные от правила и от умолчания.
    head(ws2, ["Модель Enger", "Бар", "кВт", "IP"] + PROPS +
              ["взято из выгрузки", "Блок на сайте (как есть)", "Откуда блок"])
    for r in rows:
        p = r["props"]
        i = ws2.max_row + 1
        raw = r.get("block_raw") or ""
        src = ("сайт" if block_class(raw) else
               ("правило по коду" if p.get("блок") else "не указан"))
        ws2.append([r["family"], p["bar"], p["kw"],
                    "/".join(sorted(p["ip"])) if p["ip"] else ""] +
                   [p.get(k) or DEFAULTS[k] for k in PROPS] +
                   [sum(1 for k in PROPS if p.get(k) is not None), raw, src])
        for j, k in enumerate(PROPS, 5):
            if p.get(k) is None:
                ws2.cell(i, j).font = GREY              # серым — то, что подставлено умолчанием
        if src != "сайт":
            ws2.cell(i, ws2.max_column).font = GREY
    ws2.freeze_panes = "B2"
    ws2.column_dimensions["A"].width = 26
    ws2.column_dimensions[get_column_letter(ws2.max_column - 1)].width = 30
    ws2.column_dimensions[get_column_letter(ws2.max_column)].width = 18

    # --- ЛИСТ «Все совпадения» --------------------------------------------------------
    ws3 = wb.create_sheet("Все совпадения")
    head(ws3, ["Модель Enger", "Бар", "Наша цена", "Бренд", "Модель конкурента", "Цена",
               "Разница", "На умолчании шаблона", "Ссылка"])
    for r in rows:
        for b in BRANDS:
            for c, weak in sorted(r["hits"].get(b, []),
                                  key=lambda t: (t[0]["price"] is None, t[0]["price"] or 0)):
                i = ws3.max_row + 1
                ws3.append([r["family"], r["props"]["bar"], r["o"].get("price"), TITLE[b],
                            model_label(c["name"]), c["price"],
                            (r["o"]["price"] - c["price"]) / c["price"]
                            if c["price"] and r["o"].get("price") else None,
                            ", ".join(weak), c.get("url")])
                ws3.cell(i, 9).font = LINK
                if c.get("url"):
                    ws3.cell(i, 9).hyperlink = c["url"]
                ws3.cell(i, 7).number_format = "0%"
    ws3.freeze_panes = "A2"
    for col, w in (("A", 26), ("E", 34), ("H", 30), ("I", 40)):
        ws3.column_dimensions[col].width = w

    # --- ЛИСТ «Другая комплектация» ---------------------------------------------------
    ws4 = wb.create_sheet("Другая комплектация")
    head(ws4, ["Модель Enger", "Бар", "Наша цена", "Бренд", "Модель конкурента", "Цена",
               "Чем отличается", "Ссылка"])
    for r in rows:
        for b in BRANDS:
            if r["hits"].get(b):        # строгий аналог у бренда есть — базовая версия не нужна
                continue
            for c, diff in sorted(r["base"].get(b, []),
                                  key=lambda t: (t[0]["price"] is None, t[0]["price"] or 0))[:2]:
                i = ws4.max_row + 1
                ws4.append([r["family"], r["props"]["bar"], r["o"].get("price"), TITLE[b],
                            model_label(c["name"]), c["price"], "; ".join(diff), c.get("url")])
                if c.get("url"):
                    ws4.cell(i, 8).hyperlink, ws4.cell(i, 8).font = c["url"], LINK
    ws4.freeze_panes = "A2"
    for col, w in (("A", 26), ("E", 34), ("G", 46), ("H", 40)):
        ws4.column_dimensions[col].width = w

    # --- ЛИСТ «Аналог по классу давления» ----------------------------------------------
    # Отдельно, а не в «Сводной»: тут давление приведено к ряду шаблона (наши 8,6 бар против
    # их 8), то есть допуск, которого заказчик просил не делать. Без этого листа 323 строки
    # безмасляных серий не сцеплялись вовсе — паспортных 7,5/8,5/8,6/12,5 бар в ряду нет.
    ws45 = wb.create_sheet("Аналог по классу давления")
    head(ws45, ["Модель Enger", "Наш бар", "Класс ряда", "Наша цена", "Бренд",
                "Модель конкурента", "Их бар", "Цена", "Разница", "Ссылка"])
    for r in rows:
        for b in BRANDS:
            if r["hits"].get(b) or r["soft"].get(b) or r["base"].get(b):
                continue
            for c, cls in sorted(r["ladder"].get(b, []),
                                 key=lambda t: (t[0]["price"] is None, t[0]["price"] or 0))[:2]:
                i = ws45.max_row + 1
                ws45.append([r["family"], r["props"]["bar"], "/".join(cls), r["o"].get("price"),
                             TITLE[b], model_label(c["name"]), c["props"]["bar"], c["price"],
                             (r["o"]["price"] - c["price"]) / c["price"]
                             if c["price"] and r["o"].get("price") else None, c.get("url")])
                ws45.cell(i, 9).number_format = "0%"
                if c.get("url"):
                    ws45.cell(i, 10).hyperlink, ws45.cell(i, 10).font = c["url"], LINK
    ws45.freeze_panes = "A2"
    for col, w in (("A", 26), ("F", 34), ("J", 40)):
        ws45.column_dimensions[col].width = w

    # --- ЛИСТ «Кандидаты (сайт молчит)» ------------------------------------------------
    ws5 = wb.create_sheet("Кандидаты (сайт молчит)")
    head(ws5, ["Модель Enger", "Бар", "Наша цена", "Бренд", "Модель конкурента", "Цена",
               "Молчат свойства", "Ссылка"])
    for r in rows:
        for b in BRANDS:
            if r["hits"].get(b):
                continue
            for c, weak in sorted(r["soft"].get(b, []),
                                  key=lambda t: (t[0]["price"] is None, t[0]["price"] or 0))[:2]:
                i = ws5.max_row + 1
                ws5.append([r["family"], r["props"]["bar"], r["o"].get("price"), TITLE[b],
                            model_label(c["name"]), c["price"], ", ".join(weak), c.get("url")])
                if c.get("url"):
                    ws5.cell(i, 8).hyperlink, ws5.cell(i, 8).font = c["url"], LINK
    ws5.freeze_panes = "A2"
    for col, w in (("A", 26), ("E", 34), ("G", 46), ("H", 40)):
        ws5.column_dimensions[col].width = w

    # --- ЛИСТ «Покрытие и правила» -----------------------------------------------------
    ws6 = wb.create_sheet("Покрытие и правила")
    ws6.column_dimensions["A"].width = 22
    ws6.column_dimensions["B"].width = 104
    by_brand = Counter()
    priced = Counter()
    for c in cands:
        by_brand[c["brand"]] += 1
        priced[c["brand"]] += bool(c["price"])
    hit_brand = Counter()
    for r in rows:
        for b in r["hits"]:
            hit_brand[b] += 1
    n = len(rows)
    cov = Counter()
    for r in rows:
        if r["hits"]:
            cov["строгий"] += 1
        elif r["base"]:
            cov["комплектация"] += 1
        elif r["soft"]:
            cov["молчит"] += 1
        elif r["ladder"]:
            cov["класс бар"] += 1
        else:
            cov["ничего"] += 1
    lines = [
        ("ПОКРЫТИЕ", f"строк (карточек Enger) всего {n}"),
        ("  строгий аналог", f'{cov["строгий"]} ({cov["строгий"]/n:.0%}) — лист «Сводная»'),
        ("  др. комплектация", f'{cov["комплектация"]} — только на листе «Другая комплектация»'),
        ("  сайт молчит", f'{cov["молчит"]} — только на листе «Кандидаты (сайт молчит)»'),
        ("  класс давления", f'{cov["класс бар"]} — только на листе «Аналог по классу давления»'),
        ("  кандидата нет", f'{cov["ничего"]} ({cov["ничего"]/n:.0%}) — у собранных заводов такой '
                            f"машины нет вовсе. Это почти целиком безмасляные серии сухого "
                            f"сжатия (OF, OFS, OFSA, OFSZ) на 3–5 и 20–40 бар."),
        ("", ""),
        ("Правило сцепки", "13 свойств шаблона, точное совпадение. кВт и бар обязательны "
                           "с обеих сторон. Десять свойств, выделенных в шаблоне зелёным, при "
                           "молчании сайта заменяются первым значением столбца."),
        ("Степень защиты", "в шаблоне зелёным НЕ выделена, поэтому умолчание IP23 не "
                           "подставляем: у Hansmann и Xeleron IP не указан ни на одной карточке, "
                           "и подстановка отсекла бы бренд целиком. IP режет пару только при "
                           "явном конфликте обеих сторон."),
        ("Строка отчёта", "на листе «Сводная по барам» — карточка Enger = модель + "
                          "давление: у 86 семейств из 460 цена по барам различается "
                          "(LC-18,5DFRE-500: 921 086 / 1 173 338 / 1 466 672 ₽)."),
        ("Цены", "рубли, с сайтов производителей. В образце заказчика цены в долларах по "
                 "курсу 78,9 (лист «Лист1» образца: =A1/78.9). Наша цена — из каталожной "
                 "выгрузки, не из прайс-фида: файлы расходятся на 1 141 позиции."),
        ("Цены собраны", "24–25.08. Проверка 25.08 показала дрейф +0,44% за сутки у BERG, "
                         "GMP и ET — перед отправкой заказчику пересобирайте."),
        ("ЦВЕТА «Сводной»", "три независимых слоя, их легко перепутать:"),
        ("  заливки блоков", "охра — колонка модели и блок расчёта, голубой — наша цена, "
                             "серый в тройке — аналога у бренда нет. От чисел не зависят."),
        ("  цены брендов", "градиент цветами образца, но отсчёт от НАШЕЙ цены. Пять "
                           "ступеней по отношению «их цена / наша»: меньше 0,7 — красный "
                           "F8696B; 0,7–0,9 — розовый FBAA77; 0,9–1,1 (примерно наша цена) "
                           "— жёлтый FFEB84; 1,1–1,4 — салатовый B1D47F; больше 1,4 — "
                           "зелёный 63BE7B. Красный всегда значит «конкурент дешевле нас», "
                           "насыщенность показывает насколько. Границы сняты с данных: по "
                           "2 061 паре квантили отношения 0,55 / 0,78 / 0,97 / 1,21 / 1,58."),
        ("  колонки «разница»", "разница = (наша цена − цена конкурента) / цена конкурента. "
                                "Больше нуля (мы дороже) — красный, меньше нуля (мы дешевле) — "
                                "зелёный. Так же в образце: там 714 правил, местами с "
                                "перевёрнутой полярностью, но по приоритету Excel побеждает "
                                "красный на плюсе — 129 ячеек против 68."),
        ("Курсив в тройке", "модель сцеплена с опорой на умолчание шаблона, а не на данные сайта."),
        ("", ""),
        ("Серым в «Свойствах»", "значение подставлено умолчанием шаблона, а не прочитано с сайта."),
        ("Класс давления", "паспортных 7,5 / 8,5 / 8,6 / 12,5 бар (наши безмасляные LUF, OFSA, "
                           "OFS) в ряду шаблона нет, а у заводов те же машины стоят на 7/8/10/12. "
                           "Такие пары вынесены на отдельный лист: в «Сводной» допусков нет."),
        ("", ""),
        ("Remeza", "remeza.com — сайт завода, цен нет ни в разделах, ни в карточках "
                   "(проверено 24.08). Колонка оставлена пустой: брать цену с площадки нельзя."),
        ("Xeleron", "xelerone.com публикует характеристики 76 моделей, но карточки с ценой "
                    "только три (Z7.5A, Z10A, Z175A) — остальное «цена по запросу»."),
        ("Комплектации", "ATOM, Ironmac и Kraftmachine (кроме КМ11) исполнений «на ресивере "
                         "с осушителем» на сайте не публикуют вовсе — по строгому ключу "
                         "аналога у них нет. Их базовые машины — лист «Другая комплектация»."),
        ("", ""),
        ("Бренд", "карточек с сайта / из них с ценой / наших строк, где нашёлся аналог"),
    ]
    for k, v in lines:
        ws6.append([k, v])
    for b in BRANDS:
        ws6.append([TITLE[b], f"{by_brand.get(b,0)} / {priced.get(b,0)} / {hit_brand.get(b,0)}"])
    for i in range(1, ws6.max_row + 1):
        ws6.cell(i, 1).font = Font(bold=True)
        ws6.cell(i, 2).alignment = Alignment(wrap_text=True, vertical="top")

    # Таблицы заказчика — чтобы формула «накрутка» и колонка «Реальная пр-ть» имели источник
    copy_table(wb, "накрутки", _HERE / "data" / "enger_nakrutki.csv")
    copy_table(wb, "реальная пр-ть", _HERE / "data" / "enger_realnaya_prt.csv")
    svodnaya_etalon(wb, rows, real)            # первый лист — раскладка эталона
    wb.save(out)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--proba", action="store_true", help="только 10 моделей из образца")
    ap.add_argument("--sravnit", action="store_true", help="сравнить два режима умолчаний")
    a = ap.parse_args()
    if a.sravnit:
        for strict in (True, False):
            report(build(proba=a.proba, strict=strict)[0],
                   "строгое умолчание" if strict else "молчание=совместимо")
        raise SystemExit(0)
    rows, cands = build(proba=a.proba, strict=True)
    report(rows, "строгое умолчание")
    path = save(rows, cands)
    print(f"-> {path}")
