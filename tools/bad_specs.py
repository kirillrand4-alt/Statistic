"""Наши карточки с физически невозможной парой «мощность / производительность».

Зачем. Матчер сравнивает кВт и производительность с допуском 6% и 4%. Если в
карточке одно из двух введено неверно, пара с конкурентом не соберётся никогда —
никакая правка алгоритма это не лечит, чинить надо в Битриксе.

Удельная мощность зависит от давления, поэтому судим ТОЛЬКО в диапазоне 6-15 бар,
где норма устойчива: 6-15 кВт на м3/мин (медиана по нашему каталогу 9.8, у
конкурентов ровно столько же). Вне этого диапазона низкая удельная мощность
законна — дожимной бустер DALGAKIRAN DBK берёт уже сжатый воздух и даёт 2.0, а
компрессор на 25-40 бар наоборот требует больше. Такие карточки не трогаем.

Что ловим. Проверка 11.08 против карточек конкурентов: у 171 нашей карточки ATMOS
производительность занижена примерно на порядок (медиана отношения 10.9, диапазон
7.1-17.4). ATMOS SEC 300/13 у нас 330 л/мин при 30 кВт, у трёх конкурентов 3 300.
Из-за этого бренд не матчится вовсе: 87 позиций ушло в GAP, спек-двойников ноль.
Никакая правка алгоритма это не лечит — чинить надо в Битриксе.

    python tools/bad_specs.py              # список в консоль
    python tools/bad_specs.py --csv out.csv
"""
from __future__ import annotations
import argparse, csv, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brand_spec_review as B

# Нижний порог 4.0, а не 4.5: двухступенчатые машины законно экономичнее —
# Dali EN-250/8 II даёт 250 кВт при 56 м3/мин, это 4.46 и это правда.
LO, HI = 4.0, 25.0          # кВт на м3/мин, действует только при 6-15 бар
BAR_LO, BAR_HI = 6.0, 15.0


def collect():
    rows = []
    for brand, cards in B.load_ours_all().items():
        for r in cards:
            kw, fl, bar = r.get("kw"), r.get("fl"), r.get("bar")
            if not (kw and fl and bar):
                continue
            if not (BAR_LO <= bar <= BAR_HI):    # вне диапазона норма другая
                continue
            mm = fl / 1000
            if mm < 0.05:                    # совсем мелочь — не судим
                continue
            sp = kw / mm
            if LO <= sp <= HI:
                continue
            rows.append(dict(brand=brand, name=r["name"], url=r["url"],
                             kw=kw, bar=bar, flow=fl, m3min=round(mm, 2), kw_per_m3=round(sp, 1),
                             hint="производительность занижена" if sp > HI else "производительность завышена или кВт занижены"))
    rows.sort(key=lambda x: -x["kw_per_m3"])
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", help="куда сохранить")
    a = ap.parse_args()
    rows = collect()
    print(f"карточек с невозможной парой кВт/производительность: {len(rows)}")
    for r in rows[:25]:
        print(f"  {r['kw_per_m3']:>7} кВт/м3мин  [{r['brand']}] {r['name'][:52]}")
        print(f"  {'':20} {r['kw']} кВт при {r['flow']:,.0f} л/мин — {r['hint']}")
    if a.csv:
        with open(a.csv, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter=";")
            w.writeheader(); w.writerows(rows)
        print(f"\n-> {a.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
