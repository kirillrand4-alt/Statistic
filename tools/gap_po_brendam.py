"""Чего у нас нет, а у конкурентов есть — по файлу на бренд, двумя архивами.

    GAP_2plus_po_brendam.zip   позицию возят ДВЕ и более площадки
    GAP_1_po_brendam.zip       возит только ОДНА

Разделение не косметическое. Замер 11.08: среди позиций, которые держат 4-6
площадок, у нас отсутствует 11-18%, среди «только у двоих» — 59%. То есть чем
шире позицию возят, тем вероятнее она нужна и нам, и список «2+» надо смотреть
первым, а «1 площадка» держать справочником.

Одна строка = один ТОВАР, а не карточка: карточки разных площадок схлопываются
той же логикой, что и матчинг (серия+номер, специи не противоречат, исполнение
сравнивается меткой). Иначе один и тот же компрессор попадал бы в список по
пять раз — по разу на площадку.

Колонка «Серия у нас» отвечает на главный вопрос по каждой строке: это дыра в
линейке, которую мы уже возим, или бренд/серия, которых у нас нет вовсе.
Первое закрывается заказом у того же поставщика, второе — отдельное решение.

Бренды без позиций файлом не становятся.

    python tools/gap_po_brendam.py                 # -> gap_brands/ + два zip
    python tools/gap_po_brendam.py --out /tmp/g
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
import spec_match as S

HDRS = ["№", "Товар у конкурентов (у нас нет)", "Площадок", "Где есть",
        "Цена мин", "Цена макс", "кВт", "бар", "л/мин", "Ресивер", "Серия у нас"]
WIDTHS = [5, 62, 10, 34, 12, 12, 8, 7, 9, 9, 22]


def cname(c) -> str:
    return c.get("name") or B.slug(c["url"]).replace("_", " ")


def collect(ours: dict, cands: dict):
    """(бренд -> строки для 2+, бренд -> строки для 1)."""
    matched: set[str] = set()
    for brand in sorted(set(ours) & set(cands)):
        by = defaultdict(list)
        for c in cands[brand]:
            by[c["sn"]].append(c)
        for o in ours[brand]:
            m = B.receiver_filter(o.get("rv"), B.ff_filter(o.get("ff"),
                B.cool_filter(o.get("cool"), B.ip_filter(o.get("ip"),
                S.match(o, by.get(o["sn"], [])))), o.get("name", "")))
            matched.update(c["url"] for c in B.variant_filter(o.get("name", ""), m, brand))

    many, single = defaultdict(list), defaultdict(list)
    for brand, rows in cands.items():
        our_sn = {o["sn"] for o in ours.get(brand, [])}
        by_sn = defaultdict(list)
        for c in rows:
            by_sn[c["sn"]].append(c)
        for pool in by_sn.values():
            used = set()
            for i, x in enumerate(pool):
                if id(x) in used:
                    continue
                grp = [x]; used.add(id(x))
                xm = S.variant_letters(cname(x), brand)
                for y in pool[i + 1:]:
                    if id(y) in used or y["site"] == x["site"]:
                        continue
                    if not S.match(x, [y]):
                        continue
                    if not S.same_variant(xm, S.variant_letters(cname(y), brand)):
                        continue
                    grp.append(y); used.add(id(y))
                if any(c["url"] in matched for c in grp):
                    continue                      # у нас есть — это не GAP
                sites = sorted({c["site"] for c in grp})
                pr = [c["price"] for c in grp if c.get("price")]
                best = min(grp, key=lambda c: c.get("price") or 10**12)
                row = (best, len(sites), ", ".join(sites),
                       min(pr) if pr else None, max(pr) if pr else None,
                       "да, линейка есть" if x["sn"] in our_sn else "нет ни серии, ни бренда")
                (many if len(sites) >= 2 else single)[brand].append(row)
    return many, single


def write(brand: str, rows: list, path: Path) -> int:
    if not rows:
        return 0
    rows.sort(key=lambda r: (-r[1], str(r[0]["sn"][0]), r[0]["sn"][1]))
    st = B._styles()
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Нет у нас"
    B._hdr(ws, HDRS, st)
    r = 1
    for best, nsites, where, pmin, pmax, ser in rows:
        r += 1
        ws.cell(r, 1, r - 1)
        nm = ws.cell(r, 2, cname(best)[:70]); nm.hyperlink = best["url"]; nm.font = st["blue"]
        ws.cell(r, 3, nsites); ws.cell(r, 4, where)
        for col, v in ((5, pmin), (6, pmax)):
            if v:
                ws.cell(r, col, v).number_format = "# ##0"
        ws.cell(r, 7, best.get("kw")); ws.cell(r, 8, best.get("bar"))
        ws.cell(r, 9, best.get("fl")); ws.cell(r, 10, best.get("rv"))
        ws.cell(r, 11, ser)
    for i, w in enumerate(WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(HDRS))}{r}"
    wb.save(path)
    return len(rows)


def dump(data: dict, outdir: Path, suffix: str, zpath: Path) -> tuple[int, int]:
    made = []
    for brand in sorted(data):
        title = brand.capitalize() if brand != "ir" else "IngersollRand"
        p = outdir / f"{title}_{suffix}.xlsx"
        n = write(brand, data[brand], p)
        if n:
            made.append((title, n, p))
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for _, _, p in made:
            z.write(p, p.name)
    for t, n, _ in sorted(made, key=lambda x: -x[1])[:12]:
        print(f"    {t:16}{n:>6}")
    return len(made), sum(n for _, n, _ in made)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="gap_brands")
    ap.add_argument("--zipdir", default=None)
    a = ap.parse_args()
    outdir = Path(a.out); outdir.mkdir(parents=True, exist_ok=True)
    zdir = Path(a.zipdir) if a.zipdir else outdir.parent
    zdir.mkdir(parents=True, exist_ok=True)

    ours = B.load_ours_all(); cands = B.load_comp_all()
    many, single = collect(ours, cands)

    print("возят 2+ площадки (топ брендов):")
    b1, n1 = dump(many, outdir, "net_u_nas_2plus", zdir / "GAP_2plus_po_brendam.zip")
    print(f"  брендов: {b1} | позиций: {n1:,}\n")
    print("возит 1 площадка (топ брендов):")
    b2, n2 = dump(single, outdir, "net_u_nas_1sait", zdir / "GAP_1sait_po_brendam.zip")
    print(f"  брендов: {b2} | позиций: {n2:,}")
    print(f"\n-> {zdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
