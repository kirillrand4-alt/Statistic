"""Аудит НАШИХ данных: что в каталоге противоречит фактам или самому себе.

Зачем отдельный инструмент. Матчер уже умеет говорить «эта карточка сматчилась, но
спека не подтверждена» (card_issue), однако видно это только внутри бренд-отчёта и
только по сматченным парам. Здесь тот же приём применён ко ВСЕМУ каталогу и дополнен
проверками, которым конкуренты вообще не нужны: каталог часто спорит сам с собой.

Классы проверок и на чём каждый пойман (замер 18.08, 19 105 наших карточек):

  1. Спор с площадками (538). Наше значение не подтверждает никто, а на другом
     значении сходятся >= 2 независимых сайта. Проверка агентами 18.08 на выборке из
     30 карточек: 23 подтверждены живыми паспортами, 7 оказались сравнением с СОСЕДНИМ
     SKU той же серии — от них защищает правило «значение из артикула не оспаривается»
     (см. card_issue): «ABAC FORMULA MEI37-10» сравнивался с их MEI37-13, «Dalgakiran
     TIDY-N 25-10» с 25-13, «Atmos PDP 190-14» с PDP 190-12.
  1а. Масштаб x10 (207 карточек, из них 172 ATMOS — 47% бренда). Производительность
     даёт меньше 40 л/мин на киловатт при норме 100-170: в поле «л/мин» записаны
     м3/мин, умноженные на 100 вместо 1000. Агенты подтвердили арифметику точно на
     15 карточках: 1310 против 13100 у ST 110 Vario/13, 930 против 9300 у ST 75/13,
     2350 против 23500 у AIRMAN PDSF830S-W. Живая страница сайта при этом показывает
     ПРАВИЛЬНОЕ число — расходятся выгрузка и сайт, значит чинить надо свойство 22571.
  2. Константа в поле (ММЗ). Одно значение кВт на всех 13 карточках бренда при
     производительности от 3 500 до 12 000 л/мин: у ПВ-6/0,7 стоит 77 кВт, хотя его
     Д-243 даёт 60. Ловим по доле одного значения и числу разных моделей под ним.
  3. Дубли со спорными спеками (62). Один и тот же полный код модели у двух наших
     карточек, а спека разная: «Cross Air CA15-10RA-500» 10 бар против такой же
     «…, IP54» 8 бар; «CA15-8GA-500» 15 кВт против 16.
  4. Смесь алфавитов внутри слова (239). «СБ4-24.OLD20СK» — кириллическая С и
     латинская K в одном токене; «REMEZA ВP 10-30», «KМ-24. OLD20KМ». Такие имена
     не находятся ни поиском по сайту, ни матчером.
  5. Имя против поля (27). Объём ресивера в имени не совпадает с полем: у
     «Enger AP-SRL11XA-300 10» в имени 300 л, в поле — 1.
  6. Пустые ключевые поля (193). Без кВт / бар / л-мин карточка не матчится вовсе.

Проверки, ПРОВЕРЕННЫЕ И ОТКЛОНЁННЫЕ (не добавляем, чтобы не возвращаться):
  * «кВт == бар» — 126 срабатываний, почти все верные: у REMEZA ВК20Е-15 и правда
    15 кВт и 15 бар, совпадение случайное.
  * «номер серии похож на мощность» — 802 срабатывания, ложные: у Remeza ВК15Е
    номер линейки не мощность (ВК15 = 11 кВт), это заводская схема.
  * «одинаковые спеки, разные имена» — 2 251 группа, в основном законные исполнения
    (СБ4-100.OLD15СТ и СТМ, ВК10Е-10 и ВК10Т-10), шум перекрывает пользу.

    python tools/audit_ours.py                 # -> audit_ours.xlsx
    python tools/audit_ours.py -o /tmp/a.xlsx
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import openpyxl
from openpyxl.utils import get_column_letter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brand_spec_review as B
import spec_match as S

# Коридор «сколько литров в минуту даёт киловатт»: медиана по каталогу 145, но у
# бустеров и низконапорных он законно другой, поэтому годится только как подсказка,
# а не как приговор — в отчёт идёт вместе со спором площадок.
FLOW_PER_KW = (55, 220)


def spor_s_ploshchadkami(ours, cands):
    """Наше значение не подтверждено, а на другом сходятся 2+ сайта (см. card_issue)."""
    out = []
    for brand in sorted(set(ours) & set(cands)):
        by = defaultdict(list)
        for c in cands[brand]:
            by[c["sn"]].append(c)
        for o in ours[brand]:
            iss = S.card_issue(o, by.get(o["sn"], []))
            if not iss:
                continue
            label, ov, v1, nd, ratio, src = iss
            out.append(dict(brand=brand, name=o["name"], url=o["url"], pole=label,
                            nashe=ov, u_konkurentov=v1, saitov=nd, krat=round(ratio, 2),
                            primer=src["url"]))
    return sorted(out, key=lambda r: -r["krat"])


def konstanta_v_pole(ours, min_cards=8, min_share=0.5, min_models=3):
    """Одно и то же значение кВт на многих РАЗНЫХ моделях бренда = поле не заполняли."""
    out = []
    for b, lst in ours.items():
        if len(lst) < min_cards:
            continue
        filled = [o for o in lst if o.get("kw")]
        if not filled:
            continue
        val, n = Counter(o["kw"] for o in filled).most_common(1)[0]
        models = {o["sn"] for o in filled if o["kw"] == val}
        if n / len(filled) >= min_share and len(models) >= min_models:
            flows = sorted({o["fl"] for o in filled if o["kw"] == val and o.get("fl")})
            out.append(dict(brand=b, znachenie=val, kartochek=n, vsego=len(filled),
                            modeley=len(models),
                            proizv_ot=flows[0] if flows else None,
                            proizv_do=flows[-1] if flows else None,
                            primer=next(o["url"] for o in filled if o["kw"] == val)))
    return out


def dubli_so_sporom(ours):
    """Один код модели — две наши карточки с разной спекой."""
    grp = defaultdict(list)
    for b, lst in ours.items():
        for o in lst:
            k = S._mkey(o["name"], b)
            if k:
                grp[(b, k)].append(o)
    out = []
    for (b, _), lst in grp.items():
        if len(lst) < 2:
            continue
        for f, lbl, tol in (("kw", "кВт", .06), ("bar", "бар", .04), ("fl", "л/мин", .04)):
            vals = [o[f] for o in lst if o.get(f)]
            if len(vals) > 1 and max(vals) / min(vals) - 1 > tol:
                out.append(dict(brand=b, pole=lbl,
                                znacheniya=" / ".join(f"{v:g}" for v in sorted(set(vals))),
                                imena=" || ".join(o["name"] for o in lst[:3]),
                                url=lst[0]["url"]))
                break
    return out


def smes_alfavitov(ours):
    """Кириллица и латиница внутри ОДНОГО слова — имя не найдётся ни поиском, ни матчером."""
    out = []
    for lst in ours.values():
        for o in lst:
            bad = [w for w in re.findall(r"[a-zа-яё]+", o["name"].lower())
                   if re.search(r"[а-яё]", w) and re.search(r"[a-z]", w)]
            if bad:
                out.append(dict(brand=o["brand"], name=o["name"], url=o["url"],
                                tokeny=", ".join(sorted(set(bad))[:3])))
    return out


def imya_protiv_polya(ours):
    """Что написано в имени, того нет в полях (или наоборот)."""
    out = []
    for lst in ours.values():
        for o in lst:
            nm = (o["name"] or "").lower()
            ipn = S.ip_class(nm)
            if ipn and o.get("ip") and str(ipn) != str(o["ip"]):
                out.append(dict(brand=o["brand"], name=o["name"], url=o["url"],
                                chto=f"в имени IP{ipn}, в поле IP{o['ip']}"))
            if re.search(r"без\s+ресивер", nm) and o.get("rv"):
                out.append(dict(brand=o["brand"], name=o["name"], url=o["url"],
                                chto=f"в имени «без ресивера», в поле {o['rv']:g} л"))
            m = re.search(r"[-\s](\d{3})\s*л?\b", nm)
            if m and o.get("rv") and float(m.group(1)) in (200, 270, 300, 500, 900) \
                    and abs(float(m.group(1)) - o["rv"]) > 1:
                out.append(dict(brand=o["brand"], name=o["name"], url=o["url"],
                                chto=f"в имени {m.group(1)} л, в поле {o['rv']:g} л"))
    return out


def pustye_polya(ours):
    out = []
    for lst in ours.values():
        for o in lst:
            net = [lbl for f, lbl in (("kw", "кВт"), ("bar", "бар"), ("fl", "л/мин"))
                   if not o.get(f)]
            if net:
                out.append(dict(brand=o["brand"], name=o["name"], url=o["url"],
                                pusto=", ".join(net)))
    return out


def kopipast_gabaritov(ours, min_kw_diff=0.25):
    """Одни габариты на карточках, которые физически не могут быть одного размера.

    Габариты заполняют копированием соседней карточки: ATLAS COPCO XAVS 330 E и XAVS
    336 E стоят с одним весом 4193 кг и одним корпусом, хотя дают 18 600 и 20 000 л/мин,
    а те же габариты повторены у XATS 900 E при весе 3511 кг. Ловим только грубый
    случай — разброс мощности внутри группы больше четверти: линейка давлений одного
    типоразмера (7,5/8,5/10/13 бар) корпус и правда делит, и это не ошибка."""
    grp = defaultdict(list)
    for b, lst in ours.items():
        for o in lst:
            if o.get("dim") and o.get("kw"):
                grp[(b, o["dim"])].append(o)
    out = []
    for (b, dim), lst in grp.items():
        kws = sorted({o["kw"] for o in lst})
        if len(lst) < 2 or len(kws) < 2 or kws[-1] / kws[0] - 1 < min_kw_diff:
            continue
        out.append(dict(brand=b, gabarity="×".join(f"{x:g}" for x in dim),
                        kartochek=len(lst), kvt=" / ".join(f"{k:g}" for k in kws),
                        imena=" || ".join(o["name"] for o in lst[:3]), url=lst[0]["url"]))
    return sorted(out, key=lambda r: -r["kartochek"])


def _sheet(wb, title, rows, headers, widths, link_key="url"):
    ws = wb.create_sheet(title)
    st = B._styles()
    B._hdr(ws, list(headers.values()), st)
    for i, r in enumerate(rows, start=2):
        for ci, k in enumerate(headers, start=1):
            v = r.get(k)
            cell = ws.cell(i, ci, v)
            if k in ("name", "imena") and r.get(link_key):
                cell.hyperlink = r[link_key]
                cell.font = st["blue"]
            if isinstance(v, (int, float)) and k in ("nashe", "u_konkurentov", "proizv_ot", "proizv_do"):
                cell.number_format = "# ##0.##"
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(len(rows)+1, 2)}"
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default="audit_ours.xlsx")
    a = ap.parse_args()

    ours = B.load_ours_all()
    cands = B.load_comp_all()
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    itog = []

    spor = spor_s_ploshchadkami(ours, cands)
    grubye = [r for r in spor if r["krat"] >= 3]
    melkie = [r for r in spor if r["krat"] < 3]
    h = dict(brand="Бренд", name="Наш товар", pole="Поле", nashe="У нас",
             u_konkurentov="У конкурентов", saitov="Сайтов", krat="Кратность",
             primer="Пример карточки")
    w = [14, 56, 10, 12, 14, 8, 10, 60]
    itog.append(("Грубые расхождения (>=x3)", _sheet(wb, "Грубые расхождения", grubye, h, w)))
    itog.append(("Спор с площадками (<x3)", _sheet(wb, "Спор с площадками", melkie, h, w)))

    itog.append(("Константа в поле кВт", _sheet(
        wb, "Константа в поле", konstanta_v_pole(ours),
        dict(brand="Бренд", znachenie="Значение кВт", kartochek="Карточек",
             vsego="Всего с кВт", modeley="Разных моделей", proizv_ot="Произв. от",
             proizv_do="Произв. до", primer="Пример карточки"),
        [14, 14, 11, 12, 15, 12, 12, 60], link_key="primer")))

    itog.append(("Дубли со спорной спекой", _sheet(
        wb, "Дубли со спорной спекой", dubli_so_sporom(ours),
        dict(brand="Бренд", pole="Поле", znacheniya="Значения", imena="Карточки", url="Ссылка"),
        [14, 8, 18, 90, 60])))

    itog.append(("Смесь алфавитов", _sheet(
        wb, "Смесь алфавитов", smes_alfavitov(ours),
        dict(brand="Бренд", name="Наш товар", tokeny="Спорные токены", url="Ссылка"),
        [14, 60, 20, 60])))

    itog.append(("Имя против поля", _sheet(
        wb, "Имя против поля", imya_protiv_polya(ours),
        dict(brand="Бренд", name="Наш товар", chto="Что не сходится", url="Ссылка"),
        [14, 60, 34, 60])))

    itog.append(("Пустые ключевые поля", _sheet(
        wb, "Пустые поля", pustye_polya(ours),
        dict(brand="Бренд", name="Наш товар", pusto="Не заполнено", url="Ссылка"),
        [14, 60, 18, 60])))

    itog.append(("Копипаст габаритов", _sheet(
        wb, "Копипаст габаритов", kopipast_gabaritov(ours),
        dict(brand="Бренд", gabarity="Габариты, мм", kartochek="Карточек",
             kvt="Мощности в группе", imena="Карточки", url="Ссылка"),
        [14, 20, 11, 22, 90, 60])))

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    for name, n in itog:
        print(f"  {name:<28}{n:>6}")
    print(f"\n-> {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
