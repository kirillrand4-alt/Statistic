"""Экспорт базы обзвона (только таблица call_company) в отдельный файл —
без остальной 11-гиговой статистики.

    python scripts/export_callbase.py                      # обе базы -> data/callbase/obzvon_<дата>.xlsx
    python scripts/export_callbase.py --base kc            # только Компрессор Центр
    python scripts/export_callbase.py --base meyer --format csv
    python scripts/export_callbase.py --out C:\obzvon.xlsx # свой путь

Строки идут в порядке очереди обзвона (приоритет ↓). Первая колонка — «База».
"""
from __future__ import annotations

import argparse
import csv
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.db.base import SessionLocal, init_db  # noqa: E402
from app.db.models import CallCompany  # noqa: E402
from app.services import callbase  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (поле модели, заголовок в файле) — порядок колонок выгрузки
COLS = [
    ("inn", "ИНН"), ("ogrn", "ОГРН"), ("kpp", "КПП"), ("okpo", "ОКПО"),
    ("name_short", "Краткое"), ("name_full", "Полное"), ("status", "Статус"),
    ("reg_date", "ДатаРег"), ("address", "Адрес"), ("region", "Регион"), ("opf", "ОПФ"),
    ("capital", "УстКапитал"), ("director", "Директор"), ("director_inn", "ИННдир"),
    ("founders", "Учредители"), ("okved_main", "ОсновнойОКВЭД"), ("okved_all", "ВсеОКВЭД"),
    ("phones", "Телефоны"), ("emails", "Emails"), ("sites", "Сайты"),
    ("site_phones", "Телефоны с сайта"), ("site_emails", "Email с сайта"),
    ("fin_year", "ГодОтч"), ("revenue", "Выручка"), ("profit", "ЧистПрибыль"),
    ("equity", "Капитал"), ("staff", "ССЧ"),
    ("priority", "Итоговый балл приоритета"), ("max_hit", "Макс. балл по связке"),
    ("equipment", "Оборудование по основному ОКВЭД"),
    ("equipment_all", "Все категории оборудования"),
    ("okved_hits", "Найденные ОКВЭД"), ("calc_comment", "Комментарий расчёта"),
    ("revenue_num", "Выручка, руб."), ("rank_metric", "Приоритет × выручка / 10000"),
]
HEADER = ["База"] + [title for _f, title in COLS]


def _rows(db, base):
    """Компании базы в порядке очереди обзвона (rank ↓, приоритет ↓, выручка ↓)."""
    C = CallCompany
    stmt = (select(C).where(C.base == base)
            .order_by(C.rank_metric.desc().nulls_last(), C.priority.desc(),
                      C.revenue_num.desc().nulls_last(), C.id))
    return db.execute(stmt).scalars().all()


def _record(base_label, c):
    out = [base_label]
    for field, _t in COLS:
        v = getattr(c, field, None)
        out.append("" if v is None else v)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="all", help="kc | meyer | all (по умолч. all)")
    ap.add_argument("--format", default="csv", choices=("csv", "xlsx"))
    ap.add_argument("--out", default=None,
                    help="файл или папка (иначе data/callbase/). Если указана папка — "
                         "имя obzvon_<база>_<дата>.<ext> формируется внутри неё")
    a = ap.parse_args()

    if a.base != "all" and a.base not in callbase.BASES:
        sys.exit(f"Неизвестная база: {a.base}. Доступны: {', '.join(callbase.BASES)} или all.")
    bases = list(callbase.BASES) if a.base == "all" else [a.base]

    init_db()
    db = SessionLocal()
    try:
        stamp = datetime.date.today().isoformat()
        fname = f"obzvon_{a.base}_{stamp}.{a.format}"
        # --out: папка (существует, или без расширения, или со слэшем на конце) -> имя внутри неё
        if not a.out:
            out = os.path.join(REPO, "data", "callbase", fname)
        elif (os.path.isdir(a.out) or a.out.endswith(("\\", "/"))
              or not os.path.splitext(a.out)[1]):
            out = os.path.join(a.out, fname)
        else:
            out = a.out
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

        total = 0
        if a.format == "xlsx":
            import openpyxl
            wb = openpyxl.Workbook(write_only=True)   # потоковая запись — ок и для 30k+ строк
            ws = wb.create_sheet("Обзвон")
            ws.append(HEADER)
            for base in bases:
                label = callbase.BASES[base]
                for c in _rows(db, base):
                    ws.append(_record(label, c))
                    total += 1
            wb.save(out)
        else:
            # utf-8-sig + ; — корректно открывается в Excel с кириллицей
            with open(out, "w", encoding="utf-8-sig", newline="") as fh:
                w = csv.writer(fh, delimiter=";")
                w.writerow(HEADER)
                for base in bases:
                    label = callbase.BASES[base]
                    for c in _rows(db, base):
                        w.writerow(_record(label, c))
                        total += 1

        by_base = ", ".join(f"{callbase.BASES[b]}: {callbase.count(db, b)}" for b in bases)
        print(f"Готово: {total} строк ({by_base})\nФайл: {out}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
