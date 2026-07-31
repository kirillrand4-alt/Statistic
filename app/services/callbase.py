"""База обзвона потенциальных клиентов (страницы «Обзвон …»).

Источник — выгрузки Checko: либо .xlsx с готовым расчётом приоритетов
(лист с колонками «ИНН … Итоговый балл приоритета … Приоритет × выручка / 10000»),
либо плоский .tsv/.csv с теми же базовыми колонками (тогда очередь сортируется
по выручке). Очередь: rank_metric ↓, priority ↓, выручка ↓.

«Удалить и следующая» — жёсткое удаление строки; на всякий случай полная копия
дописывается в data/callbase/deleted_<base>.tsv (страховка от случайного клика).

ПОСТРАНИЧНАЯ ВЫДАЧА (161k строк в объединённой базе). До этого очередь строилась
так: SELECT всех строк базы -> фильтрация в Python -> список id целиком в памяти
(кэш на каждую комбинацию фильтров). На 7000 компаний это терпимо, на 161k —
секунды на запрос и десятки мегабайт на каждую комбинацию фильтров, причём кэш
сбрасывался при каждом «Удалить и следующая». Теперь фильтры целиком в SQL
(см. ``derived``), а страница берётся LIMIT/OFFSET по индексу, повторяющему
ORDER BY, — см. ``page`` и ``_queue_stmt``.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import re
from dataclasses import dataclass

from sqlalchemy import func, select, text as sql_text
from sqlalchemy.orm import Session

from app.db.models import CallCompany

log = logging.getLogger(__name__)

# slug базы -> название страницы
BASES = {"kc": "Компрессор Центр", "meyer": "Meyer"}

DATA_DIR = os.environ.get("CALLBASE_DATA", os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "callbase"))

# Русский заголовок выгрузки -> поле модели. Колонки приоритета опциональны.
HEADERS = {
    "ИНН": "inn", "ОГРН": "ogrn", "КПП": "kpp", "ОКПО": "okpo",
    "Краткое": "name_short", "Полное": "name_full", "Статус": "status",
    "ДатаРег": "reg_date", "Адрес": "address", "ОПФ": "opf",
    "УстКапитал": "capital", "Директор": "director", "ИННдир": "director_inn",
    "Учредители": "founders", "ОсновнойОКВЭД": "okved_main", "ВсеОКВЭД": "okved_all",
    "Телефоны": "phones", "Emails": "emails", "Сайты": "sites",
    "ГодОтч": "fin_year", "Выручка": "revenue", "ЧистПрибыль": "profit",
    "Капитал": "equity", "ССЧ": "staff",
    "Итоговый балл приоритета": "priority",
    "Макс. балл по одной связке": "max_hit",
    "Оборудование по основному ОКВЭД": "equipment",
    "Все найденные категории оборудования": "equipment_all",
    "Найденные ОКВЭД из справочника": "okved_hits",
    "Комментарий расчёта": "calc_comment",
    "Выручка, руб. (расчет)": "revenue_num",
    "Приоритет × выручка / 10000": "rank_metric",
}

# Мусор, который Checko тащит в колонку «Сайты» (счётчики/CDN/магазин расширений).
_JUNK_SITE = re.compile(
    r"yastatic\.net|an\.yandex\.|mc\.yandex\.|avatars\.mds\.yandex|ads\.adfox\."
    r"|chrome\.google\.com|checko\.ru|yandex\.st|google-analytics|googletagmanager",
    re.I)

_MULT = {"млрд": 1e9, "млн": 1e6, "тыс": 1e3}

# «Уникальные контакты с сайта компании» — колонки, где Checko кладёт телефоны/почту,
# найденные прямо на сайте фирмы (отдельно от реестровых «Телефоны»/«Emails»).
# Заголовок таких колонок не из HEADERS и содержит «сайт» + признак контакта.
_SITE_CONTACT_HINT = ("контакт", "тел", "e-mail", "email", "почт", "мейл", "мэйл", "@")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{8,}\d")


def _norm_phone(raw: str) -> str:
    """RU-номер → канон «+7XXXXXXXXXX» (11 цифр). Иначе '' (мусор/не РФ)."""
    d = re.sub(r"\D", "", raw)
    if len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    elif len(d) == 10:
        d = "7" + d
    return "+" + d if len(d) == 11 and d[0] == "7" else ""


def extract_site_contacts(text) -> tuple[list[str], list[str]]:
    """Из свободного текста «контакты с сайта» вынуть УНИКАЛЬНЫЕ телефоны
    (канон +7…) и email (нижний регистр), сохраняя порядок появления."""
    s = str(text or "")
    emails, seen_e = [], set()
    for m in _EMAIL_RE.findall(s):
        e = m.lower().strip(".")
        if e not in seen_e:
            seen_e.add(e)
            emails.append(e)
    phones, seen_p = [], set()
    for m in _PHONE_RE.findall(_EMAIL_RE.sub(" ", s)):  # email вырезаем — их цифры не телефоны
        p = _norm_phone(m)
        if p and p not in seen_p:
            seen_p.add(p)
            phones.append(p)
    return phones, emails


def parse_money(text) -> float | None:
    """«55,3 млрд руб.» → 55.3e9; «424 тыс. руб.» → 424000; «0 руб.» → 0; '' → None."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).replace("\xa0", " ").replace(" ", " ").strip()
    if not s:
        return None
    m = re.search(r"(-?\d[\d\s]*(?:[.,]\d+)?)", s)
    if not m:
        return None
    num = float(m.group(1).replace(" ", "").replace(",", "."))
    for word, k in _MULT.items():
        if word in s:
            return num * k
    return num


def clean_sites(raw) -> str:
    """Убрать мусорные URL из «Сайты», оставить « | »-строку реальных сайтов."""
    if not raw:
        return ""
    out, seen = [], set()
    for tok in str(raw).split("|"):
        u = tok.strip()
        if not u or _JUNK_SITE.search(u):
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
    return " | ".join(out)


_POSTCODE = re.compile(r"^\d{5,6}$")
# служебные слова региона — со строчной (кроме первого слова), остальные — с заглавной
_REGION_LOWER = {"область", "обл", "край", "округ", "автономный", "автономная",
                 "автономное", "район", "г", "го", "имени", "и"}


def norm_region(s) -> str:
    """Единый регистр региона, чтобы «москва»/«Москва»/«МОСКВА» не плодили дубли:
    «москва»→«Москва», «Московская Область»→«Московская область»,
    «санкт-петербург»→«Санкт-Петербург», «республика татарстан»→«Республика Татарстан»."""
    s = re.sub(r"\s+", " ", str(s or "").strip())
    if not s:
        return ""
    out = []
    for i, w in enumerate(s.split(" ")):
        if i > 0 and w.casefold() in _REGION_LOWER:
            out.append(w.casefold())
        else:  # капитализация с учётом дефиса: санкт-петербург → Санкт-Петербург
            out.append("-".join((p[:1].upper() + p[1:].casefold()) if p else p
                                for p in w.split("-")))
    return " ".join(out)


def region_from_address(addr) -> str:
    """Регион из адреса Checko: «664043, Иркутская область, г. Иркутск…» →
    «Иркутская область»; «117545, г. Москва, …» → «Москва». Регистр нормализуется."""
    for part in str(addr or "").split(","):
        p = part.strip()
        if not p or _POSTCODE.match(p):
            continue
        p = re.sub(r"^(г\.\s*о\.|г\.|город)\s+", "", p).strip()
        return norm_region(p)
    return ""


def _num(v, as_int=False):
    try:
        n = float(str(v).replace(" ", "").replace(",", "."))
        return int(n) if as_int else n
    except (TypeError, ValueError):
        return None


def _clean_cell(v) -> str:
    # ​ — зеро-виз пробел из xlsx; float из Excel («402501001.0») — назад в строку
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).replace("​", "").strip()


def _rows_from_matrix(matrix) -> list[dict]:
    """Список списков (первая подходящая строка — заголовки) -> записи модели."""
    head_i, head = None, None
    for i, row in enumerate(matrix[:10]):  # заголовок ищем в первых строках листа
        cells = [_clean_cell(c) for c in row]
        if "ИНН" in cells and ("Краткое" in cells or "Полное" in cells):
            head_i, head = i, cells
            break
    if head is None:
        return []
    idx = {j: HEADERS[h] for j, h in enumerate(head) if h in HEADERS}
    # колонки «контакты с сайта компании»: заголовок не из HEADERS, содержит «сайт»
    # + признак контакта (плоскую колонку URL «Сайты» не трогаем — она в idx)
    site_cols = [j for j, h in enumerate(head)
                 if j not in idx and h and "сайт" in h.casefold()
                 and any(w in h.casefold() for w in _SITE_CONTACT_HINT)]
    out = []
    for row in matrix[head_i + 1:]:
        rec: dict = {}
        for j, field in idx.items():
            val = row[j] if j < len(row) else None
            if field in ("priority", "max_hit"):
                rec[field] = _num(val, as_int=True) or 0
            elif field in ("revenue_num", "rank_metric"):
                rec[field] = _num(val)
            else:
                rec[field] = _clean_cell(val)
        if not any(rec.get(k) for k in ("inn", "name_short", "name_full")):
            continue  # пустая строка
        rec["sites"] = clean_sites(rec.get("sites"))
        rec["region"] = region_from_address(rec.get("address"))
        if site_cols:
            blob = " | ".join(_clean_cell(row[j]) for j in site_cols if j < len(row))
            sp, se = extract_site_contacts(blob)
            rec["site_phones"] = " | ".join(sp)
            rec["site_emails"] = " | ".join(se)
        if rec.get("revenue_num") is None:
            rec["revenue_num"] = parse_money(rec.get("revenue"))
        if rec.get("rank_metric") is None:
            rec["rank_metric"] = (rec.get("priority") or 0) * (rec.get("revenue_num") or 0) / 10000
        out.append(rec)
    return out


def parse_upload(filename: str, data: bytes) -> list[dict]:
    """Разобрать загруженный файл (xlsx / tsv / csv) в записи для импорта."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        for ws in wb.worksheets:  # ищем лист с компаниями (есть колонка ИНН)
            matrix = [list(r) for r in ws.iter_rows(values_only=True)]
            rows = _rows_from_matrix(matrix)
            if rows:
                return rows
        return []
    # текстовые: utf-8 (с BOM) либо cp1251; разделитель — таб, иначе ; иначе ,
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1251", errors="replace")
    first = text.splitlines()[0] if text.splitlines() else ""
    delim = "\t" if "\t" in first else (";" if ";" in first else ",")
    matrix = list(csv.reader(io.StringIO(text), delimiter=delim))
    return _rows_from_matrix(matrix)


def import_rows(db: Session, base: str, rows: list[dict]) -> tuple[int, int]:
    """Добавить записи в базу ``base``; дубли по ИНН (и безINNые — по названию)
    пропускаются. -> (добавлено, пропущено)."""
    existing_inn = {inn for (inn,) in db.execute(
        select(CallCompany.inn).where(CallCompany.base == base, CallCompany.inn != "")).all()}
    existing_name = {n for (n,) in db.execute(
        select(CallCompany.name_short).where(CallCompany.base == base)).all() if n}
    added = skipped = 0
    for rec in rows:
        inn = rec.get("inn") or ""
        if inn and inn in existing_inn or (not inn and rec.get("name_short") in existing_name):
            skipped += 1
            continue
        if inn:
            existing_inn.add(inn)
        if rec.get("name_short"):
            existing_name.add(rec["name_short"])
        # производные поля считаем здесь: на чтении (161k строк, постранично)
        # пересчитывать их в Python уже нельзя — фильтры должны идти в SQL
        db.add(CallCompany(base=base, **rec, **derived(rec)))
        added += 1
    db.commit()
    _bump_version()
    return added, skipped


# Кэши на процесс: количество строк под фильтрами и агрегаты для выпадающих
# списков (регионы/ОКВЭД/оборудование). Агрегаты считаются по всей базе — на
# 161k строк это дорого, поэтому кешируем и пересчитываем только при изменении
# данных (импорт/очистка) через _bump_version.
#
# Кэша СПИСКА id больше нет: он держал в памяти список из всех подходящих id на
# КАЖДУЮ комбинацию фильтров (161k int ≈ 6 МБ на комбинацию, словарь без
# ограничения размера) и обнулялся на каждом удалении строки — то есть первый же
# клик «Удалить и следующая» заставлял пересканировать базу заново.
_count_cache: dict[tuple, int] = {}
_agg_cache: dict[tuple, list] = {}
_COUNT_CACHE_MAX = 256  # комбинаций фильтров; больше — просто чистим целиком


def _bump_version(aggregates: bool = True) -> None:
    """Данные изменились. ``aggregates=False`` — удалена одна строка: количество
    в очереди пересчитать надо, а списки регионов/ОКВЭД/оборудования (полный скан
    базы) из-за минус одной компании пересчитывать незачем — их счётчики в
    выпадающих списках справочные."""
    _count_cache.clear()
    if aggregates:
        _agg_cache.clear()


def _cached_agg(base: str, kind: str, fn):
    key = (base, kind)
    v = _agg_cache.get(key)
    if v is None:
        v = fn()
        _agg_cache[key] = v
    return v


def has_mobile(phones) -> bool:
    """Есть ли сотовый: номер с девятки (+79…/89…)."""
    for tok in str(phones or "").split("|"):
        d = re.sub(r"\D", "", tok)
        if len(d) == 11 and d[:2] in ("79", "89"):
            return True
    return False


def derived(rec) -> dict:
    """Производные поля строки — всё, что SQLite не умеет считать на кириллице
    быстро и индексируемо. Считаются ОДИН раз при записи (импорт/миграция), а на
    чтении дают обычные SQL-условия вместо фильтрации 161k строк в Python.

    ``rec`` — словарь записи из выгрузки или ORM-объект CallCompany.
    Блобы склеиваются через \\n и приводятся casefold(): побайтовый LIKE по такой
    строке с так же приведённой подстрокой = регистронезависимый поиск, который
    в SQLite иначе для кириллицы недоступен. Разделитель \\n гарантирует, что
    подстрока не «склеится» через границу полей (в поле формы \\n не ввести)."""
    get = rec.get if isinstance(rec, dict) else (lambda k: getattr(rec, k, None))
    parts = (get("name_short"), get("name_full"), get("inn"),
             get("address"), get("director"))
    equip = (get("equipment"), get("equipment_all"))
    phones = str(get("phones") or "")
    return {
        "search_blob": "\n".join(str(p) for p in parts if p).casefold(),
        "equipment_blob": "\n".join(str(p) for p in equip if p).casefold(),
        "is_active": 1 if "действующ" in str(get("status") or "").casefold() else 0,
        # раньше условие было «phones != ''»; strip() дополнительно отсекает
        # строки из одних пробелов — телефоном они всё равно не были
        "has_phone": 1 if phones.strip() else 0,
        "has_mobile": 1 if has_mobile(phones) else 0,
    }


# Порядок очереди обзвона. Повторён колонка-в-колонку в индексах ix_cc_queue /
# ix_cc_queue_active — только тогда LIMIT/OFFSET идёт по индексу без сортировки
# всей базы. Менять порядок здесь = менять индексы в models.py и ensure_schema.
def _order_by():
    C = CallCompany
    # nulls_last не пишем: в SQLite NULL меньше всего, значит при DESC он и так
    # оказывается в конце, а явное «NULLS LAST» мешает планировщику взять индекс.
    return (C.rank_metric.desc(), C.priority.desc(), C.revenue_num.desc(), C.id)


def _filtered(stmt, base: str, q="", region="", okved="", equipment="",
              hit_from=0, hit_to=0, rank_from=0.0, rank_to=0.0,
              rev_from=0.0, rev_to=0.0, only_phone=True, active_only=True,
              mobile_only=False):
    """Навесить фильтры очереди на любой запрос (страница / COUNT). Все условия
    идут в SQL — ничего не фильтруется в Python, иначе постраничность бессмысленна
    (пришлось бы вычитать всю базу, чтобы узнать, что попало на страницу)."""
    C = CallCompany
    stmt = stmt.where(C.base == base)
    if region:
        stmt = stmt.where(C.region == region)
    if okved:
        stmt = stmt.where(C.okved_main == okved)
    if hit_from:  # ОКВЭД со связкой такого балла встретился хотя бы раз
        stmt = stmt.where(C.max_hit >= hit_from)
    if hit_to:
        stmt = stmt.where(C.max_hit <= hit_to)
    if rank_from:  # общий приоритет = балл по всем ОКВЭД × выручка (/10000)
        stmt = stmt.where(C.rank_metric >= rank_from)
    if rank_to:
        stmt = stmt.where(C.rank_metric <= rank_to)
    if rev_from:
        stmt = stmt.where(C.revenue_num >= rev_from * 1e6)
    if rev_to:
        stmt = stmt.where(C.revenue_num <= rev_to * 1e6)
    if only_phone:
        stmt = stmt.where(C.has_phone == 1)
    if active_only:
        stmt = stmt.where(C.is_active == 1)
    if mobile_only:
        stmt = stmt.where(C.has_mobile == 1)
    # autoescape — % и _ из строки поиска не должны стать джокерами LIKE
    q = (q or "").strip()
    if q:
        stmt = stmt.where(C.search_blob.contains(q.casefold(), autoescape=True))
    equipment = (equipment or "").strip()
    if equipment:
        stmt = stmt.where(C.equipment_blob.contains(equipment.casefold(), autoescape=True))
    return stmt


def _queue_stmt(base: str, **flt):
    """Запрос строк базы под фильтры, в порядке очереди обзвона."""
    return _filtered(select(CallCompany), base, **flt).order_by(*_order_by())


def regions(db: Session, base: str) -> list[tuple[str, int]]:
    """Регионы базы с количеством компаний (для выпадающего списка), по убыванию."""
    def _q():
        C = CallCompany
        rows = db.execute(select(C.region, func.count()).where(C.base == base, C.region != "")
                          .group_by(C.region).order_by(func.count().desc(), C.region)).all()
        return [(r, n) for r, n in rows if r]
    return _cached_agg(base, "regions", _q)


def okved_sort_key(okved: str) -> list[int]:
    """Ключ числового порядка кода ОКВЭД: «06.10»→[6,10], «24.1»→[24,1],
    «24.10.3»→[24,10,3] (чтобы 6.10 < 24.1 < 24.10 < 25.11, а не по строке)."""
    m = re.match(r"\s*([\d.]+)", str(okved or ""))
    parts = []
    for seg in (m.group(1).strip(".").split(".") if m else []):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    return parts


def okveds(db: Session, base: str) -> list[tuple[str, int]]:
    """Основные ОКВЭД базы (код с расшифровкой) с количеством — в числовом
    порядке кода (06.10, 24.1, 25.11, 43.11…), а не по количеству."""
    def _q():
        C = CallCompany
        rows = db.execute(select(C.okved_main, func.count())
                          .where(C.base == base, C.okved_main != "")
                          .group_by(C.okved_main)).all()
        out = [(o, n) for o, n in rows if o]
        out.sort(key=lambda on: okved_sort_key(on[0]))
        return out
    return _cached_agg(base, "okveds", _q)


def equipments(db: Session, base: str) -> list[tuple[str, int]]:
    """Категории оборудования базы (из расчёта: основная + все найденные)
    с количеством компаний, по убыванию.

    Считаем по ГРУППАМ, а не по строкам: различных сочетаний
    (equipment, equipment_all) в базе пара сотен, поэтому SQL схлопывает 161k
    строк до ~250 групп, а Python разбирает « | »-списки только у них. Раньше
    разбор шёл по каждой строке — 1,2 с на первый показ страницы после старта
    сервиса (список категорий нужен выпадающему фильтру)."""
    def _q():
        from collections import Counter
        C = CallCompany
        cnt: Counter = Counter()
        for eq, eq_all, n in db.execute(
                select(C.equipment, C.equipment_all, func.count())
                .where(C.base == base)
                .group_by(C.equipment, C.equipment_all)).all():
            for cat in set(split_list(eq)) | set(split_list(eq_all)):
                cnt[cat] += n
        return sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))
    return _cached_agg(base, "equipments", _q)


# Колонки, дозаводимые на живой базе (имя -> тип). Порядок важен: производные
# добавляем последними, их бэкофилл идёт отдельным проходом.
_ADD_COLUMNS = (
    ("region", "VARCHAR(96)"),
    ("max_hit", "INTEGER DEFAULT 0"),
    ("site_phones", "TEXT"),
    ("site_emails", "TEXT"),
    ("search_blob", "TEXT"),
    ("equipment_blob", "TEXT"),
    ("is_active", "INTEGER DEFAULT 0"),
    ("has_phone", "INTEGER DEFAULT 0"),
    ("has_mobile", "INTEGER DEFAULT 0"),
)

# Индексы под фактические фильтры и порядок постраничного списка. Создаются
# здесь, а не только через create_all: create_all трогает лишь НОВЫЕ таблицы,
# а боевая call_company давно существует. Определения обязаны совпадать с
# __table_args__ в app/db/models.py — иначе новая база и мигрированная разъедутся.
#
# Устройство и замеры на 161 799 строках — в комментарии к CallCompany.
_ORDER_COLS = "rank_metric DESC, priority DESC, revenue_num DESC, id"
_CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_call_company_base_inn"
    " ON call_company (base, inn)",
    f"CREATE INDEX IF NOT EXISTS ix_cc_queue ON call_company"
    f" (base, {_ORDER_COLS}, has_phone, has_mobile, is_active, max_hit)",
    f"CREATE INDEX IF NOT EXISTS ix_cc_queue_active ON call_company"
    f" (base, is_active, {_ORDER_COLS}, has_phone, has_mobile, max_hit)",
    f"CREATE INDEX IF NOT EXISTS ix_cc_base_region ON call_company"
    f" (base, region, {_ORDER_COLS}, has_phone, has_mobile, is_active, max_hit)",
    f"CREATE INDEX IF NOT EXISTS ix_cc_base_okved ON call_company"
    f" (base, okved_main, {_ORDER_COLS}, has_phone, has_mobile, is_active, max_hit)",
    # покрывающий индекс под GROUP BY в equipments(): список категорий для
    # выпадающего фильтра собирается по нему, не читая строки таблицы
    "CREATE INDEX IF NOT EXISTS ix_cc_base_equipment"
    " ON call_company (base, equipment, equipment_all)",
    # частичный индекс-«хвостик» недомигрированных строк: после миграции он пуст,
    # поэтому проверка «всё ли посчитано» на старте сервиса ничего не стоит
    "CREATE INDEX IF NOT EXISTS ix_cc_needs_backfill ON call_company (id)"
    " WHERE search_blob IS NULL",
)

# Старый индекс очереди: (base, rank_metric) по возрастанию. Порядок выдачи он не
# закрывает (три колонки из четырёх отсутствуют, направление противоположное),
# поэтому только занимал место и путал планировщик. Имя переиспользовать нельзя —
# CREATE INDEX IF NOT EXISTS оставил бы старое определение, поэтому новые зовутся
# ix_cc_*, а этот сносим.
_DROP_INDEXES = ("DROP INDEX IF EXISTS ix_call_company_queue",)

_BACKFILL_CHUNK = 2000  # строк на один commit при миграции 161k строк


def ensure_schema(db: Session) -> None:
    """Дозавести новые колонки call_company на живой базе (ALTER TABLE ADD COLUMN —
    работает и в SQLite, и в Postgres), заполнить region по адресу и производные
    поля у старых строк, создать индексы постраничного списка.

    Порядок именно такой: сначала бэкофилл, потом индексы. Наоборот 161k UPDATE'ов
    ещё и перестраивали бы четыре индекса построчно."""
    from sqlalchemy import inspect, text
    cols = {c["name"] for c in inspect(db.get_bind()).get_columns("call_company")}
    added = [n for n, _d in _ADD_COLUMNS if n not in cols]
    for name, decl in _ADD_COLUMNS:
        if name not in cols:
            db.execute(text(f"ALTER TABLE call_company ADD COLUMN {name} {decl}"))
    db.commit()
    changed = _backfill(db, fresh="search_blob" in added)
    for stmt in _DROP_INDEXES + _CREATE_INDEXES:
        db.execute(text(stmt))
    db.commit()
    if added or changed:
        # без статистики планировщик SQLite может взять не тот индекс и снова
        # уйти в сортировку всей выборки; ANALYZE делаем только после миграции,
        # на каждом старте он не нужен
        db.execute(text("ANALYZE"))
        db.commit()
        log.info("callbase: индексы очереди созданы, статистика обновлена")


def _backfill(db: Session, fresh: bool) -> int:
    """Заполнить/нормализовать регион и производные поля у строк, где их нет.
    -> сколько строк тронуто. ``fresh`` — производные колонки только что заведены
    (значит мигрировать надо всю таблицу, проверять нечего)."""
    changed = _backfill_region(db)
    if fresh or _needs_derived(db):
        changed += _backfill_derived(db)
    if changed:
        log.info("callbase: мигрировано строк call_company: %d", changed)
        _bump_version()
    return changed


def _backfill_region(db: Session) -> int:
    """Регион: NULL -> из адреса, иначе нормализовать регистр (чтобы «москва» и
    «Москва» не расходились в фильтре). Проход по всей таблице на каждом старте —
    как было и раньше, но UPDATE'ы теперь батчами, а не по одному на строку
    (на 161k это были 161k отдельных запросов)."""
    C = CallCompany
    batch, changed = [], 0
    for r in db.execute(select(C.id, C.address, C.region)):
        want = norm_region(r.region) if r.region else region_from_address(r.address)
        if want == (r.region or ""):
            continue
        batch.append({"cid": r.id, "region": want})
        changed += 1
        if len(batch) >= _BACKFILL_CHUNK:
            _flush_backfill(db, batch)
            batch = []
    _flush_backfill(db, batch)
    return changed


def _needs_derived(db: Session) -> bool:
    """Есть ли строки без производных полей. Дёшево благодаря частичному индексу
    ix_cc_needs_backfill (он пуст, когда всё мигрировано), поэтому обычный старт
    сервиса эту проверку не замечает. Нужна на случай, если прошлая миграция
    оборвалась на середине (рестарт службы) — она резюмируется."""
    return db.execute(select(CallCompany.id)
                      .where(CallCompany.search_blob.is_(None)).limit(1)).first() is not None


def _backfill_derived(db: Session) -> int:
    """Производные поля (см. derived) у строк, где их ещё нет. Коммит на каждый
    батч — прерванная миграция продолжается со следующего старта, а не начинается
    заново."""
    C = CallCompany
    batch, changed = [], 0
    for r in db.execute(select(C.id, C.name_short, C.name_full, C.inn, C.address,
                               C.director, C.equipment, C.equipment_all, C.status,
                               C.phones).where(C.search_blob.is_(None))):
        batch.append({"cid": r.id, **derived(r)})
        changed += 1
        if len(batch) >= _BACKFILL_CHUNK:
            _flush_backfill(db, batch)
            batch = []
    _flush_backfill(db, batch)
    return changed


def _flush_backfill(db: Session, batch: list[dict]) -> None:
    """Один executemany на батч вместо UPDATE на каждую строку."""
    if not batch:
        return
    cols = {k: sql_text(f":{k}") for k in batch[0] if k != "cid"}
    db.execute(CallCompany.__table__.update()
               .where(CallCompany.id == sql_text(":cid")).values(**cols), batch)
    db.commit()


# ---------------------------------------------------------------- постранично
DEFAULT_PAGE_SIZE = 50
PAGE_SIZES = (25, 50, 100, 200)  # выбор в интерфейсе; 200 строк — предел читаемости


def to_int(v, default: int = 0) -> int:
    """«12» -> 12, ""/None/мусор -> default. Номер страницы приходит из URL и
    может быть чем угодно — 422 продажнику показывать нельзя."""
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def norm_page_size(v) -> int:
    """Размер страницы из URL -> ближайший разрешённый (защита от ?size=100000)."""
    n = to_int(v, DEFAULT_PAGE_SIZE)
    return n if n in PAGE_SIZES else DEFAULT_PAGE_SIZE


@dataclass(frozen=True)
class PageResult:
    """Одна страница очереди + всё, что нужно навигации в шаблоне."""

    rows: list      # строки ЭТОЙ страницы (не всей выборки)
    total: int      # всего строк под фильтрами
    page: int       # текущая страница, 1-based, уже приведена в диапазон
    size: int
    pages: int      # всего страниц, минимум 1 (пустая выборка — тоже страница)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size

    @property
    def first_no(self) -> int:
        """Номер первой строки страницы в общей нумерации (для «строки X-Y из N»)."""
        return self.offset + 1 if self.total else 0

    @property
    def last_no(self) -> int:
        return min(self.offset + self.size, self.total)

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    def window(self, radius: int = 2) -> list[int]:
        """Номера страниц вокруг текущей: на 3236 страницах показывать все нельзя."""
        lo = max(1, self.page - radius)
        hi = min(self.pages, self.page + radius)
        return list(range(lo, hi + 1))


def count_filtered(db: Session, base: str, **flt) -> int:
    """Сколько строк в очереди под фильтрами. Отдельный дешёвый COUNT по тем же
    условиям (по индексу, без чтения самих строк), результат кэшируется до
    ближайшего изменения данных — иначе каждая перелистнутая страница считала бы
    заново. Счёт точный: на 161k строк COUNT по индексу — единицы миллисекунд."""
    key = (base,) + tuple(sorted(flt.items()))
    n = _count_cache.get(key)
    if n is None:
        n = db.execute(_filtered(select(func.count()).select_from(CallCompany),
                                 base, **flt)).scalar() or 0
        if len(_count_cache) >= _COUNT_CACHE_MAX:
            _count_cache.clear()
        _count_cache[key] = n
    return n


def page(db: Session, base: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE,
         **flt) -> PageResult:
    """Страница очереди: LIMIT size OFFSET (page-1)*size на стороне БД.

    Почему LIMIT/OFFSET, а не keyset: интерфейсу нужен переход на произвольную
    страницу («последняя», номер страницы), а keyset умеет только «следующая от
    этой строки» — для прыжка всё равно понадобился бы OFFSET. Ключ сортировки
    здесь составной из четырёх колонок с NULL'ами (rank_metric, priority,
    revenue_num, id), keyset-условие по нему получается громоздким и легко
    ломается. При этом OFFSET дёшев ровно потому, что индекс ix_cc_queue
    повторяет ORDER BY целиком: СУБД проматывает записи индекса, не читая строки
    таблицы и не сортируя. Keyset понадобится, если база вырастет до миллионов
    строк или если появится сортировка не по индексу.

    Номер страницы приводится в диапазон 1..pages — ?page=99999 показывает
    последнюю страницу, а не пустоту и не 500.
    """
    size = norm_page_size(size)
    total = count_filtered(db, base, **flt)
    pages = max(1, -(-total // size))  # округление вверх
    page = min(max(1, to_int(page, 1)), pages)
    rows = []
    if total:
        rows = list(db.execute(_queue_stmt(base, **flt)
                               .limit(size).offset((page - 1) * size)).scalars().all())
    return PageResult(rows=rows, total=total, page=page, size=size, pages=pages)


def pick(db: Session, base: str, skip: int = 0, **flt):
    """Текущая карточка очереди: (компания | None, всего_в_очереди).

    Раньше строился полный список id очереди и брался элемент [skip]; теперь это
    одна строка через LIMIT 1 OFFSET skip по тому же индексу — базу не читаем
    целиком ни на показ карточки, ни на «Пропустить»."""
    skip = max(0, to_int(skip, 0))
    total = count_filtered(db, base, **flt)
    if skip >= total:
        return None, total
    company = db.execute(_queue_stmt(base, **flt).limit(1).offset(skip)).scalars().first()
    return company, total


def count(db: Session, base: str) -> int:
    return db.execute(select(func.count()).select_from(CallCompany)
                      .where(CallCompany.base == base)).scalar() or 0


def delete_company(db: Session, base: str, company_id: int) -> bool:
    """Удалить строку из базы, дописав её копию в deleted_<base>.tsv."""
    c = db.get(CallCompany, company_id)
    if c is None or c.base != base:
        return False
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"deleted_{base}.tsv")
    fields = ["inn", "ogrn", "kpp", "okpo", "name_short", "name_full", "status",
              "reg_date", "address", "region", "opf", "capital", "director",
              "director_inn", "founders", "okved_main", "okved_all",
              "phones", "emails", "sites", "site_phones", "site_emails",
              "fin_year", "revenue", "profit", "equity", "staff",
              "priority", "max_hit", "equipment", "equipment_all", "okved_hits",
              "calc_comment", "revenue_num", "rank_metric"]
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        if new:
            w.writerow(fields)
        w.writerow([getattr(c, f) if getattr(c, f) is not None else "" for f in fields])
    db.delete(c)
    db.commit()
    # aggregates=False: минус одна компания не стоит полного пересчёта списков
    # регионов/ОКВЭД/оборудования (скан всей базы) на каждый клик продажника
    _bump_version(aggregates=False)
    return True


def clear_base(db: Session, base: str) -> int:
    """Полностью очистить базу ``base`` (для перезаливки). -> сколько удалено."""
    n = count(db, base)
    from sqlalchemy import delete as sql_delete
    db.execute(sql_delete(CallCompany).where(CallCompany.base == base))
    db.commit()
    _bump_version()
    return n


def split_list(raw) -> list[str]:
    """« | »-строка выгрузки -> список значений."""
    return [t.strip() for t in str(raw or "").split("|") if t.strip()]


def tel_href(phone: str) -> str:
    """«+7 495 785-94-60» -> «tel:+74957859460»."""
    return "tel:" + re.sub(r"[^\d+]", "", phone)


def fmt_mln(value) -> str:
    """Выручка в рублях -> «1 234» / «0,4» млн для колонки списка; None -> «—».
    Разряды разделяем узким пробелом, дробную часть — запятой (русская запись)."""
    if value is None:
        return "—"
    mln = float(value) / 1e6
    if mln and abs(mln) < 10:
        return f"{mln:.1f}".replace(".", ",")
    return f"{mln:,.0f}".replace(",", " ")


def short_text(value, limit: int = 60) -> str:
    """Обрезать длинное значение для ячейки таблицы (ОКВЭД с расшифровкой,
    название) — иначе одна строка распирает всю страницу списка."""
    s = str(value or "").strip()
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"
