"""Матчинг ВСЕХ брендов ТОЛЬКО с compressortyt.ru (основной конкурент) — один xlsx.
Логика 1-в-1 с боевым matching_all_export (match()+фильтры ресивер/FF/охлажд/IP,
категории через match_cat), но пул кандидатов сужен до compressortyt. Статусы:
«есть у обоих» / «только у нас» / «нет у нас (GAP)» — GAP считается по compressortyt.
Ссылки кликабельные, цены числом."""
import sys, os
sys.path.insert(0,".")
from collections import defaultdict, Counter
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from brand_spec_review import load_ours_all, load_comp_all
from spec_match import match, receiver_filter, ff_filter, ip_filter, cool_filter
from category_spec_review import load_ours_cat, load_comp_cat, match_cat, CATS

OUT="/home/user/Statistic/Matching_compressortyt.xlsx"
SITE="compressortyt"
HEAD=["категория","бренд","статус","наш товар","наши спеки","цена наша",
      "карточка compressortyt","спеки конкурента","цена конкур","наша ссылка","ссылка конкурента"]
ST={"есть у обоих":0,"только у нас":1,"нет у нас (GAP)":2}
CATORD={"компрессор":0,"осушитель":1,"ресивер":2,"генератор азота":3}
CATNAME={"osushiteli":"осушитель","resivery":"ресивер","azot":"генератор азота"}

def fmt(v): return "" if v is None else (f"{v:g}" if isinstance(v,float) else str(v))
def disp(b): return "IngersollRand" if b=="ir" else b.capitalize()
def spec_comp(o):
    p=[]
    if o.get("kw") is not None: p.append(f"{fmt(o['kw'])}кВт")
    if o.get("bar") is not None: p.append(f"{fmt(o['bar'])}бар")
    if o.get("fl") is not None: p.append(f"{fmt(o['fl'])}л/мин")
    if o.get("ff"): p.append("FF")
    if o.get("vsd"): p.append("VSD")
    if o.get("rv"): p.append("ресив")
    return " ".join(p)
def spec_cat(ck,o):
    if ck=="osushiteli":
        return f"{o.get('typ') or ''} {fmt(o.get('bar'))}бар {fmt(o.get('fl'))}л/мин трТ{o.get('dp') if o.get('dp') is not None else '—'}".strip()
    if ck=="resivery":
        return f"{fmt(o.get('vol'))}л {fmt(o.get('bar'))}бар {o.get('ori') or ''}".strip()
    return f"{fmt(o.get('fl'))}л/мин {fmt(o.get('bar'))}бар чист{o.get('pur') or '—'}%"
def row(cat,b,st,o,c,specfn,*a):
    return [cat, disp(b), st,
            o.get("name","") if o else "", specfn(*a,o) if o else "",
            (float(o["price"]) if o and o.get("price") else None),
            c.get("name","") if c else "", specfn(*a,c) if c else "",
            (float(c["price"]) if c and c.get("price") else None),
            o.get("url","") if o else "", c.get("url","") if c else ""]

def build():
    rows=[]
    # ===== КОМПРЕССОРЫ (кандидаты только compressortyt) =====
    ours=load_ours_all()
    cands={b:[c for c in lst if SITE in (c.get("site") or "")] for b,lst in load_comp_all().items()}
    matched=set()
    for b in set(ours)|set(cands):
        by=defaultdict(list)
        for c in cands.get(b,[]): by[c["sn"]].append(c)
        for o in ours.get(b,[]):
            m=receiver_filter(o.get("rv"), ff_filter(o.get("ff"),
                cool_filter(o.get("cool"), ip_filter(o.get("ip"), match(o, by.get(o["sn"], []))))))
            if m:
                for c in m:
                    matched.add(c["url"])
                    rows.append(row("компрессор",b,"есть у обоих",o,c,spec_comp))
            else:
                rows.append(row("компрессор",b,"только у нас",o,None,spec_comp))
    for b,lst in cands.items():
        seen=set()
        for c in lst:
            if c["url"] in matched or c["url"] in seen: continue
            seen.add(c["url"])
            rows.append(row("компрессор",b,"нет у нас (GAP)",None,c,spec_comp))
    # ===== КАТЕГОРИИ =====
    oc=load_ours_cat()
    cc={ck:{b:[c for c in lst if SITE in (c.get("site") or "")] for b,lst in bl.items()}
        for ck,bl in load_comp_cat().items()}
    for ck in CATS:
        cat=CATNAME[ck]; O=oc.get(ck,{}); C=cc.get(ck,{}); mcat=set()
        for b,lst in O.items():
            by=defaultdict(list)
            for c in C.get(b,[]): by[c["sn"]].append(c)
            for o in lst:
                m=match_cat(ck,o,by.get(o["sn"],[]))
                if m:
                    for c in m:
                        mcat.add(c["url"])
                        rows.append(row(cat,b,"есть у обоих",o,c,spec_cat,ck))
                else:
                    rows.append(row(cat,b,"только у нас",o,None,spec_cat,ck))
        for b,lst in C.items():
            seen=set()
            for c in lst:
                if c["url"] in mcat or c["url"] in seen: continue
                seen.add(c["url"])
                rows.append(row(cat,b,"нет у нас (GAP)",None,c,spec_cat,ck))
    rows.sort(key=lambda r:(CATORD.get(r[0],9), r[1], ST.get(r[2],9), r[3] or r[6]))

    wb=openpyxl.Workbook(); ws=wb.active; ws.title="Матчинг compressortyt"
    hf=PatternFill("solid",fgColor="305496"); bd=Font(bold=True,color="FFFFFF")
    ctr=Alignment(horizontal="center",vertical="center",wrap_text=True)
    blue=Font(color="0563C1",underline="single")
    ws.append(HEAD)
    for ci in range(1,len(HEAD)+1):
        c=ws.cell(1,ci); c.font=bd; c.fill=hf; c.alignment=ctr
    for r in rows:
        ws.append(r); rr=ws.max_row
        for col in (6,9):
            if ws.cell(rr,col).value is not None: ws.cell(rr,col).number_format="# ##0"
        for col in (10,11):
            u=ws.cell(rr,col).value
            if u: ws.cell(rr,col).hyperlink=u; ws.cell(rr,col).font=blue
    ws.freeze_panes="A2"; ws.column_dimensions["D"].width=46; ws.column_dimensions["G"].width=46
    wb.save(OUT)
    cnt=Counter((r[0],r[2]) for r in rows)
    print(f"строк: {len(rows)}")
    print(f"{'категория':<16}{'есть у обоих':>14}{'только у нас':>14}{'нет у нас GAP':>15}")
    for cat in CATORD:
        if not any(k[0]==cat for k in cnt): continue
        print(f"{cat:<16}{cnt[(cat,'есть у обоих')]:>14}{cnt[(cat,'только у нас')]:>14}{cnt[(cat,'нет у нас (GAP)')]:>15}")
    print(f"-> {OUT}")

if __name__=="__main__":
    build()
