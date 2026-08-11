"""Снятые с производства у конкурентов — по файлу на бренд.

Тот же лист, что в отчётах brand_spec_review («Снятые у конкурентов»), но вынесен
отдельной книгой на каждый бренд: удобно раздавать по направлениям. Бренды, у
которых снятых нет, файлом не становятся — пустышки только мешают.

Колонки те же и в том же порядке, чтобы глаз не переучивался:
    № | Карточка конкурента (снято) | Сайт | Цена (была) | Серия |
    У нас (тот же ряд) | Наша цена

«У нас (тот же ряд)» заполняется по той же логике, что и матчинг: сначала ищем
нашу карточку, которая реально сходится с этой по серии и специям; если такой
нет, но серия в каталоге есть — пишем «серия есть у нас». Это разные сообщения:
первое значит «вот прямая замена», второе — «линейку возим, конкретной модели нет».

    python tools/snyatye_po_brendam.py                # -> snyatye/ + Snyatye.zip
    python tools/snyatye_po_brendam.py --out /tmp/sn
"""
from __future__ import annotations

import argparse
import os
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

import openpyxl
from openpyxl.utils import get_column_letter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brand_spec_review as B
from spec_match import match

HDRS = ["№", "Карточка конкурента (снято)", "Сайт", "Цена (была)", "Серия",
        "У нас (тот же ряд)", "Наша цена"]
WIDTHS = [5, 60, 18, 12, 10, 50, 12]


def build(brand: str, ours: list, cands: list, path: Path) -> int:
    sny = [c for c in cands if c.get("status") == "снято"]
    if not sny:
        return 0
    sny.sort(key=lambda c: (str(c["sn"][0]), c["sn"][1], c["site"]))
    st = B._styles()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Снятые у конкурентов"
    B._hdr(ws, HDRS, st)

    our_by_sn = defaultdict(list)
    for o in ours:
        our_by_sn[o["sn"]].append(o)
    our_sn = set(our_by_sn)

    r = 1
    for c in sny:
        r += 1
        ws.cell(r, 1, r - 1)
        nm = ws.cell(r, 2, c["name"][:70]); nm.hyperlink = c["url"]; nm.font = st["strike"]
        ws.cell(r, 3, c["site"])
        if c.get("price"):
            pc = ws.cell(r, 4, c["price"]); pc.number_format = "# ##0"; pc.font = st["strike"]
        ws.cell(r, 5, f"{str(c['sn'][0]).upper()}{c['sn'][1]:g}")
        oo = [o for o in our_by_sn.get(c["sn"], []) if match(o, [c])]
        if oo:
            o = oo[0]
            l = ws.cell(r, 6, o["name"][:50]); l.hyperlink = o["url"]; l.font = st["blue"]
            if o.get("price"):
                ws.cell(r, 7, o["price"]).number_format = "# ##0"
        elif c["sn"] in our_sn:
            ws.cell(r, 6, "серия есть у нас")
    for i, w in enumerate(WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(HDRS))}{r}"
    wb.save(path)
    return len(sny)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="snyatye")
    ap.add_argument("--zip", default=None, help="куда положить архив (по умолчанию рядом)")
    a = ap.parse_args()
    outdir = Path(a.out); outdir.mkdir(parents=True, exist_ok=True)

    ours = B.load_ours_all(); cands = B.load_comp_all()
    made = []
    for brand in sorted(set(ours) | set(cands)):
        title = brand.capitalize() if brand != "ir" else "IngersollRand"
        path = outdir / f"{title}_snyatye.xlsx"
        n = build(brand, ours.get(brand, []), cands.get(brand, []), path)
        if n:
            made.append((title, n, path))
            print(f"  {title:16}{n:>6}")
    zpath = Path(a.zip) if a.zip else outdir.parent / "Snyatye_po_brendam.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for _, _, p in made:
            z.write(p, p.name)
    print(f"\nбрендов со снятыми: {len(made)} | позиций всего: {sum(n for _, n, _ in made):,}")
    print(f"-> {zpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
