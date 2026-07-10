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
        db.add(CallCompany(base=base, **rec))
        added += 1
    db.commit()
    _bump_version()
    return added, skipped


# Кэши на процесс: очередь (id по фильтрам) и агрегаты для выпадающих списков
# (регионы/ОКВЭД/оборудование). Списки считаются по всей базе — на большой базе
# (7000+ компаний) это дорого, поэтому кешируем и пересчитываем только при
# изменении данных (импорт/удаление/очистка) через _bump_version.
_queue_cache: dict[tuple, list[int]] = {}
_agg_cache: dict[tuple, list] = {}


def _bump_version() -> None:
    _queue_cache.clear()
    _agg_cache.clear()


def _cached_agg(base: str, kind: str, fn):
    key = (base, kind)
    v = _agg_cache.get(key)
    if v is None:
        v = fn()
        _agg_cache[key] = v
    return v


def _has(hay, needle: str) -> bool:
    # casefold — корректная нечувствительность к регистру для кириллицы
    # (SQLite lower()/ilike умеет только ASCII, поэтому текст фильтруем в Python)
    return needle.casefold() in (hay or "").casefold()


def has_mobile(phones) -> bool:
    """Есть ли сотовый: номер с девятки (+79…/89…)."""
    for tok in str(phones or "").split("|"):
        d = re.sub(r"\D", "", tok)
        if len(d) == 11 and d[:2] in ("79", "89"):
            return True
    return False


def _queue_ids(db: Session, base: str, q="", region="", okved="", equipment="",
               hit_from=0, hit_to=0, rank_from=0.0, rank_to=0.0,
               rev_from=0.0, rev_to=0.0, only_phone=True, active_only=True,
               mobile_only=False) -> list[int]:
    """id компаний базы под фильтры, в порядке очереди обзвона. Точные условия
    (регион/ОКВЭД из выпадающих списков, диапазоны чисел) — в SQL; текстовый
    поиск и сотовые — в Python (SQLite не умеет кириллицу без регистра)."""
    C = CallCompany
    stmt = (select(C.id, C.name_short, C.name_full, C.inn, C.address, C.director,
                   C.equipment, C.equipment_all, C.status, C.phones)
            .where(C.base == base))
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
        stmt = stmt.where(C.phones.is_not(None), C.phones != "")
    stmt = stmt.order_by(C.rank_metric.desc().nulls_last(),
                         C.priority.desc(),
                         C.revenue_num.desc().nulls_last(),
                         C.id)
    q, equipment = (q or "").strip(), (equipment or "").strip()
    ids = []
    for r in db.execute(stmt).all():
        if active_only and "действующ" not in (r.status or "").casefold():
            continue
        if mobile_only and not has_mobile(r.phones):
            continue
        if q and not any(_has(v, q) for v in
                         (r.name_short, r.name_full, r.inn, r.address, r.director)):
            continue
        if equipment and not (_has(r.equipment, equipment) or _has(r.equipment_all, equipment)):
            continue
        ids.append(r.id)
    return ids


def regions(db: Session, base: str) -> list[tuple[str, int]]:
    """Регионы базы с количеством компаний (для выпадающего списка), по убыванию."""
    def _q():
        C = CallCompany
        rows = db.execute(select(C.region, func.count()).where(C.base == base, C.region != "")
                          .group_by(C.region).order_by(func.count().desc(), C.region)).all()
        return [(r, n) for r, n in rows if r]
    return _cached_agg(base, "regions", _q)


def okveds(db: Session, base: str) -> list[tuple[str, int]]:
    """Основные ОКВЭД базы (код с расшифровкой) с количеством, по убыванию."""
    def _q():
        C = CallCompany
        rows = db.execute(select(C.okved_main, func.count())
                          .where(C.base == base, C.okved_main != "")
                          .group_by(C.okved_main).order_by(func.count().desc(), C.okved_main)).all()
        return [(o, n) for o, n in rows if o]
    return _cached_agg(base, "okveds", _q)


def equipments(db: Session, base: str) -> list[tuple[str, int]]:
    """Категории оборудования базы (из расчёта: основная + все найденные)
    с количеством компаний, по убыванию."""
    def _q():
        from collections import Counter
        C = CallCompany
        cnt: Counter = Counter()
        for eq, eq_all in db.execute(
                select(C.equipment, C.equipment_all).where(C.base == base)).all():
            for cat in set(split_list(eq)) | set(split_list(eq_all)):
                cnt[cat] += 1
        return sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))
    return _cached_agg(base, "equipments", _q)


def ensure_schema(db: Session) -> None:
    """Дозавести новые колонки call_company на живой базе (ALTER TABLE ADD COLUMN —
    работает и в SQLite, и в Postgres) и заполнить region по адресу у старых строк."""
    from sqlalchemy import inspect, text
    cols = {c["name"] for c in inspect(db.get_bind()).get_columns("call_company")}
    if "region" not in cols:
        db.execute(text("ALTER TABLE call_company ADD COLUMN region VARCHAR(96)"))
    if "max_hit" not in cols:
        db.execute(text("ALTER TABLE call_company ADD COLUMN max_hit INTEGER DEFAULT 0"))
    if "site_phones" not in cols:
        db.execute(text("ALTER TABLE call_company ADD COLUMN site_phones TEXT"))
    if "site_emails" not in cols:
        db.execute(text("ALTER TABLE call_company ADD COLUMN site_emails TEXT"))
    db.commit()
    # заполнить/нормализовать регион у всех строк: NULL → из адреса, иначе привести
    # регистр (чтобы «москва»/«Москва» слиплись). Обновляем только отличающиеся.
    rows = db.execute(select(CallCompany.id, CallCompany.address, CallCompany.region)).all()
    changed = 0
    for cid, addr, reg in rows:
        want = norm_region(reg) if reg else region_from_address(addr)
        if want != (reg or ""):
            db.execute(CallCompany.__table__.update().where(CallCompany.id == cid)
                       .values(region=want))
            changed += 1
    if changed:
        db.commit()
        _bump_version()


def pick(db: Session, base: str, skip: int = 0, **flt):
    """Текущая карточка очереди: (компания | None, всего_в_очереди).
    Список id кэшируется на процесс — повторные показы/пропуски не сканируют базу."""
    key = (base,) + tuple(sorted(flt.items()))
    ids = _queue_cache.get(key)
    if ids is None:
        ids = _queue_ids(db, base, **flt)
        _queue_cache[key] = ids
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
    _bump_version()
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
