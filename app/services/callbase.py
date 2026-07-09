"""База обзвона потенциальных клиентов (страницы «Обзвон …»).

Источник — выгрузки Checko: либо .xlsx с готовым расчётом приоритетов
(лист с колонками «ИНН … Итоговый балл приоритета … Приоритет × выручка / 10000»),
либо плоский .tsv/.csv с теми же базовыми колонками (тогда очередь сортируется
по выручке). Очередь: rank_metric ↓, priority ↓, выручка ↓.

«Удалить и следующая» — жёсткое удаление строки; на всякий случай полная копия
дописывается в data/callbase/deleted_<base>.tsv (страховка от случайного клика).
"""
from __future__ import annotations

import csv
import io
import os
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import CallCompany

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
    out = []
    for row in matrix[head_i + 1:]:
        rec: dict = {}
        for j, field in idx.items():
            val = row[j] if j < len(row) else None
            if field == "priority":
                rec[field] = _num(val, as_int=True) or 0
            elif field in ("revenue_num", "rank_metric"):
                rec[field] = _num(val)
            else:
                rec[field] = _clean_cell(val)
        if not any(rec.get(k) for k in ("inn", "name_short", "name_full")):
            continue  # пустая строка
        rec["sites"] = clean_sites(rec.get("sites"))
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
        db.add(CallCompany(base=base, **rec))
        added += 1
    db.commit()
    return added, skipped


def _has(hay, needle: str) -> bool:
    # casefold — корректная нечувствительность к регистру для кириллицы
    # (SQLite lower()/ilike умеет только ASCII, поэтому текст фильтруем в Python)
    return needle.casefold() in (hay or "").casefold()


def _queue_ids(db: Session, base: str, q="", region="", equipment="", min_priority=0,
               min_revenue_mln=0.0, only_phone=True, active_only=True) -> list[int]:
    """id компаний базы под фильтры, в порядке очереди обзвона. Числа и сортировка —
    в SQL; текстовые фильтры — в Python (кириллица без регистра)."""
    C = CallCompany
    stmt = (select(C.id, C.name_short, C.name_full, C.inn, C.address, C.director,
                   C.equipment, C.equipment_all, C.status)
            .where(C.base == base))
    if min_priority:
        stmt = stmt.where(C.priority >= min_priority)
    if min_revenue_mln:
        stmt = stmt.where(C.revenue_num >= min_revenue_mln * 1e6)
    if only_phone:
        stmt = stmt.where(C.phones.is_not(None), C.phones != "")
    stmt = stmt.order_by(C.rank_metric.desc().nulls_last(),
                         C.priority.desc(),
                         C.revenue_num.desc().nulls_last(),
                         C.id)
    q, region, equipment = (q or "").strip(), (region or "").strip(), (equipment or "").strip()
    ids = []
    for r in db.execute(stmt).all():
        if active_only and "действующ" not in (r.status or "").casefold():
            continue
        if q and not any(_has(v, q) for v in
                         (r.name_short, r.name_full, r.inn, r.address, r.director)):
            continue
        if region and not _has(r.address, region):
            continue
        if equipment and not (_has(r.equipment, equipment) or _has(r.equipment_all, equipment)):
            continue
        ids.append(r.id)
    return ids


def pick(db: Session, base: str, skip: int = 0, **flt):
    """Текущая карточка очереди: (компания | None, всего_в_очереди)."""
    ids = _queue_ids(db, base, **flt)
    skip = max(0, skip)
    company = db.get(CallCompany, ids[skip]) if skip < len(ids) else None
    return company, len(ids)


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
              "reg_date", "address", "opf", "capital", "director", "director_inn",
              "founders", "okved_main", "okved_all", "phones", "emails", "sites",
              "fin_year", "revenue", "profit", "equity", "staff", "priority",
              "equipment", "equipment_all", "okved_hits", "calc_comment",
              "revenue_num", "rank_metric"]
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        if new:
            w.writerow(fields)
        w.writerow([getattr(c, f) if getattr(c, f) is not None else "" for f in fields])
    db.delete(c)
    db.commit()
    return True


def clear_base(db: Session, base: str) -> int:
    """Полностью очистить базу ``base`` (для перезаливки). -> сколько удалено."""
    n = count(db, base)
    from sqlalchemy import delete as sql_delete
    db.execute(sql_delete(CallCompany).where(CallCompany.base == base))
    db.commit()
    return n


def split_list(raw) -> list[str]:
    """« | »-строка выгрузки -> список значений."""
    return [t.strip() for t in str(raw or "").split("|") if t.strip()]


def tel_href(phone: str) -> str:
    """«+7 495 785-94-60» -> «tel:+74957859460»."""
    return "tel:" + re.sub(r"[^\d+]", "", phone)
