"""ПОЛНЫЙ матчинг всех ссылок между собой (наши <-> конкуренты), актуальный, по всем
брендам и категориям. Три статуса на каждую ссылку:
  «есть у обоих»     — наш товар сматчился с карточкой конкурента (пара ссылок);
  «только у нас»     — наш товар, у конкурентов матча нет;
  «нет у нас (GAP)»  — карточка конкурента без матча у нас (можно завести).
Логика матчинга 1-в-1 с боевыми выгрузками: compressors = match()+фильтры (ресивер/FF/
охлажд/IP), категории = match_cat(). Один общий CSV + по файлу на категорию, всё в zip."""
import csv, sys, os, zipfile
csv.field_size_limit(sys.maxsize)
from collections import defaultdict, Counter
from brand_spec_review import load_ours_all, load_comp_all
from spec_match import match, receiver_filter, ff_filter, ip_filter, cool_filter
from category_spec_review import load_ours_cat, load_comp_cat, match_cat, CATS

OUTDIR="/home/user/Statistic/matching"; ZIP="/home/user/Statistic/Matching_all.zip"
ALL_NAME="ВСЁ_матчинг.csv"
HEAD=["категория","бренд","статус","наш товар","наши спеки","цена наша",
      "сайт конкурента","карточка конкурента","спеки конкурента","цена конкур",
      "наша ссылка","ссылка конкурента"]
ST={"есть у обоих":0,"только у нас":1,"нет у нас (GAP)":2}
CATORD={"компрессор":0,"осушитель":1,"ресивер":2,"генератор азота":3}
CATNAME={"osushiteli":"осушитель","resivery":"ресивер","azot":"генератор азота"}

def fmt(v): return "" if v is None else (f"{v:g}" if isinstance(v,float) else str(v))
def fmtp(v): return "" if v is None else f"{float(v):.0f}"
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
    """строка пары/одиночки: o — наш (или None для GAP), c — конкурент (или None)."""
    return [cat, disp(b), st,
            o.get("name","") if o else "", specfn(*a,o) if o else "", fmtp(o.get("price")) if o else "",
            c.get("site","") if c else "", c.get("name","") if c else "", specfn(*a,c) if c else "",
            fmtp(c.get("price")) if c else "",
            o.get("url","") if o else "", c.get("url","") if c else ""]

def build():
    os.makedirs(OUTDIR, exist_ok=True)
    rows=[]
    # ===== КОМПРЕССОРЫ =====
    ours=load_ours_all(); cands=load_comp_all(); matched=set()
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
    for b,lst in cands.items():                       # GAP: конкуренты без матча
        seen=set()
        for c in lst:
            if c["url"] in matched or c["url"] in seen: continue
            seen.add(c["url"])
            rows.append(row("компрессор",b,"нет у нас (GAP)",None,c,spec_comp))

    # ===== КАТЕГОРИИ (осушители/ресиверы/азот) =====
    oc=load_ours_cat(); cc=load_comp_cat()
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

    rows.sort(key=lambda r:(CATORD.get(r[0],9), r[1], ST.get(r[2],9), r[3] or r[7]))

    # общий файл + по категории
    made=[]
    def w(path,data):
        with open(path,"w",encoding="utf-8-sig",newline="") as fh:
            wr=csv.writer(fh,delimiter=";"); wr.writerow(HEAD); wr.writerows(data)
        return len(data)
    allpath=os.path.join(OUTDIR,ALL_NAME); w(allpath,rows); made.append(allpath)
    for cat in CATORD:
        sub=[r for r in rows if r[0]==cat]
        if not sub: continue
        p=os.path.join(OUTDIR,f"{cat.replace(' ','_')}_матчинг.csv"); w(p,sub); made.append(p)

    with zipfile.ZipFile(ZIP,"w",zipfile.ZIP_DEFLATED) as z:
        for p in made: z.write(p, os.path.basename(p))

    # сводка: категория × статус (число — по правилам)
    cnt=Counter((r[0],r[2]) for r in rows)
    print(f"ВСЕГО строк матчинга: {len(rows)}")
    print(f"{'категория':<16}{'есть у обоих':>14}{'только у нас':>14}{'нет у нас GAP':>15}")
    for cat in CATORD:
        if not any(k[0]==cat for k in cnt): continue
        print(f"{cat:<16}{cnt[(cat,'есть у обоих')]:>14}{cnt[(cat,'только у нас')]:>14}{cnt[(cat,'нет у нас (GAP)')]:>15}")
    print(f"-> {ZIP}")

if __name__=="__main__":
    build()
