"""Страницы НАШЕГО сайта (prokompressor.ru) по брендам: один CSV на бренд +
один общий список по порядку. Источник — те же загрузчики, что и спек-матч
(load_ours_all = компрессоры, load_ours_cat = осушители/ресиверы/азот), чтобы
список совпадал с тем, что реально участвует в матчинге. Конкуренты тут не нужны.
CSV utf-8-sig, ';' — как остальные выгрузки (unmatched/dropped)."""
import csv, sys, os, zipfile
csv.field_size_limit(sys.maxsize)
from collections import defaultdict
from brand_spec_review import load_ours_all
from category_spec_review import load_ours_cat

OUTDIR="/home/user/Statistic/our_pages"; ZIP="/home/user/Statistic/Our_pages_by_brand.zip"
ALL_NAME="_ВСЕ_бренды_по_порядку.csv"

# единая шапка: компрессорные колонки + «доп.спеки» под категорийную специфику
# (осушитель: тип/точка росы; ресивер: ориентация; азот: чистота) — чтобы один бренд
# = один файл со ВСЕМИ его страницами, а не три разных схемы.
HEAD=["категория","наш товар","серия","кВт","бар","произв,л/мин","ресивер,л",
      "FF","VSD","масло","привод","вес,кг","доп.спеки","цена","ссылка"]
CAT_ORDER={"компрессор":0,"осушитель":1,"ресивер":2,"азот":3}   # порядок внутри бренда

def fmt(v): return "" if v is None else (f"{v:g}" if isinstance(v,float) else str(v))
def fmtp(v): return "" if v is None else f"{float(v):.0f}"   # цена — рубли целиком, без 1.1e+06
def ser(o): sn=o["sn"]; return f"{sn[0]} {sn[1]:g}"
def disp(b): return "IngersollRand" if b=="ir" else b.capitalize()   # как в brand_spec_review

def row_comp(o):
    return ["компрессор", o.get("name",""), ser(o), fmt(o.get("kw")), fmt(o.get("bar")),
            fmt(o.get("fl")), fmt(o.get("rv")), "FF" if o.get("ff") else "",
            "VSD" if o.get("vsd") else "", o.get("oil") or "", o.get("dr") or "",
            fmt(o.get("we")), "", fmtp(o.get("price")), o.get("url","")]

def row_cat(ck, o):
    if ck=="osushiteli":
        cat="осушитель"; res=""; dop=f"тип:{o.get('typ') or '—'} тр:{o.get('dp') if o.get('dp') is not None else '—'}"
        fl=o.get("fl")
    elif ck=="resivery":
        cat="ресивер"; res=fmt(o.get("vol")); dop=f"ориент:{o.get('ori') or '—'}"; fl=None
    else:
        cat="азот"; res=""; dop=f"чистота:{o.get('pur') or '—'}%"; fl=o.get("fl")
    return [cat, o.get("name",""), ser(o), "", fmt(o.get("bar")), fmt(fl), res,
            "", "", "", "", "", dop, fmtp(o.get("price")), o.get("url","")]

def w(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        wr=csv.writer(fh, delimiter=";"); wr.writerow(HEAD); wr.writerows(rows)
    return len(rows)

def build():
    os.makedirs(OUTDIR, exist_ok=True)
    comp=load_ours_all()                 # brand -> [компрессоры]
    cat=load_ours_cat()                  # cat -> brand -> [товары]
    per=defaultdict(list)                # brand -> [строки]
    for b,lst in comp.items():
        per[b]+=[row_comp(o) for o in lst]
    for ck,bm in cat.items():
        for b,lst in bm.items(): per[b]+=[row_cat(ck,o) for o in lst]

    made=[]; summary=[]; seen_all=set(); combined=[]
    for b in sorted(per):                                 # бренды по алфавиту
        # внутри бренда: категория -> серия -> имя; dedup по ссылке (товар мог попасть
        # и в компрессоры, и в категорию при пересечении детекторов имени)
        rows=sorted(per[b], key=lambda r:(CAT_ORDER.get(r[0],9), r[2], r[1]))
        uniq=[]; seen=set()
        for r in rows:
            if r[-1] in seen: continue
            seen.add(r[-1]); uniq.append(r)
        path=os.path.join(OUTDIR, f"{disp(b)}.csv"); made.append(path)
        summary.append((disp(b), w(path, uniq)))
        for r in uniq:
            if r[-1] in seen_all: continue
            seen_all.add(r[-1]); combined.append([disp(b)]+r)   # +колонка «бренд»

    allpath=os.path.join(OUTDIR, ALL_NAME)
    with open(allpath, "w", encoding="utf-8-sig", newline="") as fh:
        wr=csv.writer(fh, delimiter=";"); wr.writerow(["бренд"]+HEAD); wr.writerows(combined)

    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(allpath, ALL_NAME)
        for p in made: z.write(p, os.path.basename(p))

    print(f"Брендов: {len(summary)} | всего страниц (общий список): {len(combined)}")
    for name,n in sorted(summary, key=lambda x:-x[1]): print(f"  {name:<16} {n}")
    print(f"-> {ZIP}  (+ общий список: {ALL_NAME})")

if __name__=="__main__":
    build()
