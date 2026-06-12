"""Экспорт ВСЕХ ссылок, не вошедших ни в один матч — наши и конкурентов, со спеками и причиной.
CSV (utf-8-sig, ';') для анализа в Excel/pandas + категории. Причина: серии нет у второй
стороны (можно добавить/у нас уникально) ИЛИ серия есть, но спеки разошлись (проверить)."""
import csv, sys, os, zipfile
csv.field_size_limit(sys.maxsize)
from collections import defaultdict
from brand_spec_review import load_ours_all, load_comp_all
from spec_match import match, receiver_filter, ff_filter
from category_spec_review import (load_ours_cat, load_comp_cat, match_cat, CATS)

OUTDIR="/home/user/Statistic/unmatched"; ZIP="/home/user/Statistic/Unmatched_all.zip"

def w(path, header, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        wr=csv.writer(fh, delimiter=";")
        wr.writerow(header); wr.writerows(rows)
    return len(rows)

def fmt(v): return "" if v is None else (f"{v:g}" if isinstance(v,float) else v)

def build():
    os.makedirs(OUTDIR, exist_ok=True)
    made=[]; summary=[]
    # ===== КОМПРЕССОРЫ =====
    ours=load_ours_all(); cands=load_comp_all()
    matched_comp=set()                 # url конкурентов, вошедшие в матч
    our_un=[]                          # наши без матча
    for b in set(ours)|set(cands):
        by=defaultdict(list)
        for c in cands.get(b,[]): by[c["sn"]].append(c)
        our_sn={o["sn"] for o in ours.get(b,[])}
        for o in ours.get(b,[]):
            m=receiver_filter(o.get("rv"), ff_filter(o.get("ff"), match(o, by.get(o["sn"], []))))
            if m:
                for c in m: matched_comp.add(c["url"])
            else:
                reason="серии нет у конкурентов" if not by.get(o["sn"]) else "серия есть, спеки разошлись"
                our_un.append([b, o["name"], f"{o['sn'][0]} {o['sn'][1]:g}", fmt(o.get("kw")),
                               fmt(o.get("bar")), fmt(o.get("fl")), "FF" if o.get("ff") else "",
                               "VSD" if o.get("vsd") else "", fmt(o.get("rv")), o.get("oil") or "",
                               o.get("dr") or "", fmt(o.get("we")), fmt(o.get("price")), reason, o["url"]])
    H_OUR=["бренд","наш товар","серия","кВт","бар","произв,л/мин","FF","VSD","ресивер,л",
           "масло","привод","вес,кг","цена","причина","ссылка"]
    # конкуренты без матча
    comp_un=[]
    for b,lst in cands.items():
        our_sn={o["sn"] for o in ours.get(b,[])}
        seen=set()
        for c in lst:
            if c["url"] in matched_comp or c["url"] in seen: continue
            seen.add(c["url"])
            reason="серии нет у нас (можно завести)" if c["sn"] not in our_sn else "серия есть у нас, спеки разошлись"
            comp_un.append([b, c["site"], c["name"], f"{c['sn'][0]} {c['sn'][1]:g}", fmt(c.get("kw")),
                            fmt(c.get("bar")), fmt(c.get("fl")), "FF" if c.get("ff") else "",
                            "VSD" if c.get("vsd") else "", fmt(c.get("rv")), fmt(c.get("price")),
                            c.get("status") or "", c.get("sku") or "", reason, c["url"]])
    H_COMP=["бренд","сайт","карточка конкурента","серия","кВт","бар","произв,л/мин","FF","VSD",
            "ресивер,л","цена","статус","артикул","причина","ссылка"]
    p1=OUTDIR+"/компрессоры_НАШИ_без_матча.csv"; made.append(p1)
    summary.append(("компрессоры наши без матча", w(p1, H_OUR, our_un)))
    p2=OUTDIR+"/компрессоры_КОНКУРЕНТЫ_без_матча.csv"; made.append(p2)
    summary.append(("компрессоры конкурентов без матча", w(p2, H_COMP, comp_un)))

    # ===== КАТЕГОРИИ (осушители/ресиверы/азот) =====
    oc=load_ours_cat(); cc=load_comp_cat()
    def cspec(ck,x):
        if ck=="osushiteli": return [fmt(x.get("typ")), fmt(x.get("fl")), fmt(x.get("bar")), fmt(x.get("dp")), ""]
        if ck=="resivery":   return ["", "", fmt(x.get("bar")), "", f"{fmt(x.get('vol'))}л {x.get('ori') or ''}"]
        return ["", fmt(x.get("fl")), fmt(x.get("bar")), "", f"чистота {fmt(x.get('pur'))}%"]
    cat_our=[]; cat_comp=[]; matched_cat=set()
    for ck in CATS:
        O=oc.get(ck,{}); C=cc.get(ck,{})
        for b,lst in O.items():
            by=defaultdict(list)
            for c in C.get(b,[]): by[c["sn"]].append(c)
            our_sn={o["sn"] for o in lst}
            for o in lst:
                m=match_cat(ck, o, by.get(o["sn"], []))
                if m:
                    for c in m: matched_cat.add(c["url"])
                else:
                    reason="серии нет у конкурентов" if not by.get(o["sn"]) else "серия есть, спеки разошлись"
                    cat_our.append([ck, b, o["name"]]+cspec(ck,o)+[fmt(o.get("price")), reason, o["url"]])
        for b,lst in C.items():
            our_sn={o["sn"] for o in O.get(b,[])}
            seen=set()
            for c in lst:
                if c["url"] in matched_cat or c["url"] in seen: continue
                seen.add(c["url"])
                reason="серии нет у нас (можно завести)" if c["sn"] not in our_sn else "серия есть у нас, спеки разошлись"
                cat_comp.append([ck, b, c.get("site",""), c["name"]]+cspec(ck,c)+[fmt(c.get("price")),
                                 c.get("status") or "", reason, c["url"]])
    HC_OUR=["категория","бренд","наш товар","тип","произв,л/мин","бар","точка росы","объём/чистота","цена","причина","ссылка"]
    HC_COMP=["категория","бренд","сайт","карточка конкурента","тип","произв,л/мин","бар","точка росы","объём/чистота","цена","статус","причина","ссылка"]
    p3=OUTDIR+"/категории_НАШИ_без_матча.csv"; made.append(p3)
    summary.append(("категории наши без матча", w(p3, HC_OUR, cat_our)))
    p4=OUTDIR+"/категории_КОНКУРЕНТЫ_без_матча.csv"; made.append(p4)
    summary.append(("категории конкурентов без матча", w(p4, HC_COMP, cat_comp)))

    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for p in made: z.write(p, os.path.basename(p))
    print("ИТОГО строк:")
    for name,n in summary: print(f"  {name:<40} {n}")
    print(f"-> {ZIP}")

if __name__=="__main__":
    build()
