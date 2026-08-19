"""Вторая нарезка того же отчёта — по ТЕМАМ, а не по брендам. НЕ боевая.

Боевой отчёт (brand_spec_review.py) даёт файл на бренд, внутри восемь листов. Это
удобно, когда работаешь с одним брендом, и неудобно, когда вопрос сквозной: «покажи
весь GAP по каталогу», «сколько всего снятых серий», «выгрузи все ложные поля на
правку в Битрикс». Ради этого и нужна вторая нарезка: архив на тему, внутри —
сводный файл по всем брендам сразу плюс те же файлы по брендам.

Инструмент СОБИРАЕТ ИЗ ГОТОВЫХ файлов brand_reports/*_spec_review.xlsx, а не считает
заново. Это принципиально: пересчёт означал бы второй источник правды, который начнёт
расходиться с боевым при первой же правке матчера. Здесь расхождение невозможно по
построению — данные те же ячейки, просто переложенные.

    python tools/reports_by_topic.py            # все темы
    python tools/reports_by_topic.py GAP1       # только одну

Сначала должен быть собран боевой отчёт: python brand_spec_review.py
"""
from __future__ import annotations

import os
import re
import sys
import zipfile
from copy import copy
from pathlib import Path

import openpyxl
from openpyxl.utils import get_column_letter

SRC = Path("/home/user/Statistic/brand_reports")
OUT = Path("/home/user/Statistic/reports_by_topic")

# Тема -> (имя архива, листы боевого отчёта). Порядок листов внутри темы значим:
# в сводном файле они станут вкладками в этом же порядке.
TOPICS = {
    "Kompressory_match": ("Компрессоры — матчинг по брендам", ["спек-матч", "неоднозначные"]),
    "GAP1":              ("GAP: одна площадка", ["GAP 1 сайт"]),
    "GAP2plus":          ("GAP: две и более площадки", ["GAP 2+ сайтов"]),
    "Ostalnye_tovary":   ("Остальные товары (осушители, ресиверы)", ["Остальные товары"]),
    "Snyatye":           ("Снятое у конкурентов", ["Снятые у конкурентов", "Снятые серии"]),
    "Lozhnye_dannye":    ("Ложные данные у нас", ["Ложные данные у нас"]),
}


def brand_of(path: Path) -> str:
    return path.name.replace("_spec_review.xlsx", "")


def copy_cell(src, dst):
    """Значение + оформление + ссылка.

    Копируем ОБЪЕКТЫ стиля (шрифт, заливка, формат), а не `cell._style`: последний —
    это набор индексов в таблицы стилей СВОЕЙ книги, и в чужой книге они указывают в
    пустоту (openpyxl падает на сохранении с IndexError в write_stylesheet).

    Ссылку копируем отдельной строкой: она живёт не в стиле, а в самой ячейке, и без
    неё отчёт теряет главное — переход на карточку товара."""
    dst.value = src.value
    if src.has_style:
        dst.font = copy(src.font)
        dst.fill = copy(src.fill)
        dst.border = copy(src.border)
        dst.alignment = copy(src.alignment)
        dst.number_format = src.number_format
    if src.hyperlink is not None:
        dst.hyperlink = copy(src.hyperlink)


def copy_sheet(ws_src, wb_dst, title, brand=None):
    """Лист целиком. Если задан brand — первой колонкой добавляется «Бренд»
    (в сводном файле без него строки разных брендов неразличимы)."""
    ws = wb_dst.create_sheet(title[:31])
    off = 1 if brand else 0
    for r, row in enumerate(ws_src.iter_rows(), start=1):
        if brand:
            ws.cell(r, 1, "Бренд" if r == 1 else brand)
        for c, cell in enumerate(row, start=1):
            copy_cell(cell, ws.cell(r, c + off))
    if brand:
        ws.column_dimensions["A"].width = 16
    for k, dim in ws_src.column_dimensions.items():
        if dim.width:
            i = openpyxl.utils.column_index_from_string(k) + off
            ws.column_dimensions[get_column_letter(i)].width = dim.width
    ws.freeze_panes = "B2" if brand else (ws_src.freeze_panes or "A2")
    if ws.max_row > 1:
        ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"
    return ws.max_row - 1


def append_rows(ws_dst, ws_src, brand, header_done):
    """Дописать строки бренда в сводный лист (шапку — только один раз)."""
    start = 1 if not header_done else 2
    for row in ws_src.iter_rows(min_row=start):
        r = ws_dst.max_row + (0 if ws_dst.max_row == 1 and ws_dst.cell(1, 1).value is None else 1)
        is_hdr = not header_done and row[0].row == 1
        ws_dst.cell(r, 1, "Бренд" if is_hdr else brand)
        for c, cell in enumerate(row, start=2):
            copy_cell(cell, ws_dst.cell(r, c))
    return True


def build(only=None):
    files = sorted(SRC.glob("*_spec_review.xlsx"))
    if not files:
        sys.exit(f"нет исходных файлов в {SRC} — сначала соберите боевой отчёт "
                 f"(python brand_spec_review.py)")
    OUT.mkdir(parents=True, exist_ok=True)
    made = []
    for key, (title, sheets) in TOPICS.items():
        if only and key not in only:
            continue
        tdir = OUT / key
        tdir.mkdir(parents=True, exist_ok=True)
        for f in tdir.glob("*.xlsx"):     # чистим прошлую сборку, иначе исчезнувший
            f.unlink()                    # бренд останется в архиве призраком
        summary = openpyxl.Workbook(); summary.remove(summary.active)
        sum_ws = {}
        total = {s: 0 for s in sheets}
        for path in files:
            b = brand_of(path)
            wb = openpyxl.load_workbook(path)
            have = [s for s in sheets if s in wb.sheetnames and wb[s].max_row > 1]
            if not have:
                wb.close(); continue
            one = openpyxl.Workbook(); one.remove(one.active)
            for s in have:
                n = copy_sheet(wb[s], one, s)
                total[s] += n
                if s not in sum_ws:
                    sum_ws[s] = summary.create_sheet(s[:31])
                    append_rows(sum_ws[s], wb[s], b, header_done=False)
                else:
                    append_rows(sum_ws[s], wb[s], b, header_done=True)
            one.save(tdir / f"{b}.xlsx")
            wb.close()
        # ширины сводных листов берём из первого попавшегося бренда — они одинаковы
        for s, ws in sum_ws.items():
            ws.freeze_panes = "B2"
            ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"
            ws.column_dimensions["A"].width = 16
        if sum_ws:
            summary.save(tdir / "00_Все_бренды.xlsx")
        zpath = Path("/home/user/Statistic") / f"Otchet_{key}.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(tdir.glob("*.xlsx")):
                z.write(f, f.name)
        n = sum(total.values())
        made.append((key, title, n, zpath))
        print(f"{key:<18} {title:<42} строк {n:>7,}  -> {zpath.name} "
              f"({zpath.stat().st_size/1e6:.1f} МБ)")
    return made


if __name__ == "__main__":
    build(set(sys.argv[1:]) or None)
