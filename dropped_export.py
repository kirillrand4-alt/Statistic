"""«Тёмная материя»: ссылки, не вошедшие НИКУДА — ни в матч, ни в список-без-матча.
Это URL, отсеянные ДО кандидатов (бренд не распознан / не компрессор и не категория /
серия не извлечена / не товарная страница / нет цифры). Со спеками, что есть, и причиной.
Наши: товары каталога, выпавшие из всех пайплайнов (компрессоры+категории)."""
import csv, sys, os, zipfile, re
csv.field_size_limit(sys.maxsize)
from collections import Counter
from atlas_need_specs import load_universe, is_product_url, slug, dm, COMPETITORS
from matcher import brand_of, brand_from_text, BRAND_ALIASES
from spec_match import (is_compressor, num, sane_kw, bar_value, flow_value, bar_flow_pairs, series_num, is_flow_key)
from brand_spec_review import (load_comp_all, ser_of, SPECS_CSV, PROKO_CSV)
from category_spec_review import cat_of, load_comp_cat

OUTDIR="/home/user/Statistic/dropped"; ZIP="/home/user/Statistic/Dropped_nowhere.zip"

def specs_kbf(d):
    kw=None; rb=rf=fk=None
    for k,v in (d or {}).items():
        kl=k.lower()
        if kw is None and "мощ" in kl and "шум" not in kl and "звук" not in kl: kw=sane_kw(num(v))
        if rb is None and "давлен" in kl: rb=v
        if rf is None and is_flow_key(k): rf=v; fk=kl
    pairs=bar_flow_pairs(rb, rf, (fk or "")+" "+str(rf or ""))
    bar,fl=(pairs[0] if pairs else (None,None))
    return kw, bar, fl

def fmt(v): return "" if v is None else (f"{v:g}" if isinstance(v,float) else v)

def guess_brand(nm):
    """Первое латинское/кириллич. слово-бренд после слова 'компрессор/осушитель/...'."""
    m=re.search(r'(?:компрессор\w*|осушител\w*|генератор\w*|ресивер\w*|станц\w*|блок)\s+'
                r'([A-Za-zА-Яа-яЁё][\w\-]{2,16})', str(nm))
    if not m: return ""
    w=m.group(1).strip("-").lower()
    return "" if w in ("воздуха","винтовой","для","азота","с","на","сжатого","высокого") else w

def comp_reason(u, nm):
    """Почему URL не стал кандидатом (порядок как в пайплайне)."""
    if not is_product_url(u): return "не товарная страница (категория/аренда/б-у/статья)"
    text=(nm or "")+" "+slug(u)
    b=brand_of(u, nm)
    if not b: return "бренд не распознан"
    if not any(ch.isdigit() for ch in text): return "нет цифры в названии"
    if not is_compressor(text):
        if cat_of(text): return "категория-товар (осушитель/ресивер/азот) — без бренда/серии"
        return "не компрессор и не категория (запчасть/аксессуар/прочее)"
    if not ser_of(text, b): return "серия+номер не извлекаются"
    return "прочее"

def build():
    os.makedirs(OUTDIR, exist_ok=True)
    names, specs, _ = load_universe()
    # кандидаты, которые ГДЕ-ТО учтены (матч или список-без-матча):
    accounted=set()
    for b,lst in load_comp_all().items():
        for c in lst: accounted.add(c["url"])
    for ck,bm in load_comp_cat().items():
        for b,lst in bm.items():
            for c in lst: accounted.add(c["url"])
    # вселенная: все товарные URL конкурентов из выгрузок
    rows=[]
    seen=set()
    for u,nm in names.items():
        if dm(u) not in COMPETITORS: continue
        if u in accounted or u in seen: continue
        seen.add(u)
        d=specs.get(u,{})
        kw,bar,fl=specs_kbf(d)
        rows.append([brand_of(u,nm) or guess_brand(nm) or "?", dm(u), nm or slug(u), fmt(kw), fmt(bar), fmt(fl),
                     "есть" if d else "нет", comp_reason(u,nm), u])
    H=["бренд(гадание)","сайт","карточка","кВт","бар","произв,л/мин","спеки сняты","причина отсева","ссылка"]
    rows.sort(key=lambda r:(r[7], r[0]))
    p1=OUTDIR+"/КОНКУРЕНТЫ_не_вошли_никуда.csv"
    with open(p1,"w",encoding="utf-8-sig",newline="") as fh:
        wr=csv.writer(fh,delimiter=";"); wr.writerow(H); wr.writerows(rows)
    print(f"конкуренты не вошли никуда: {len(rows)}")
    print("  причины:", dict(Counter(r[7] for r in rows)))

    # ===== НАШИ: товары каталога, выпавшие отовсюду =====
    catalog={}
    for r in csv.DictReader(open(SPECS_CSV, encoding="utf-8-sig", errors="replace"), delimiter=";"):
        code=(r.get("IE_CODE") or "").strip()
        if not code: continue
        cur=catalog.setdefault(code,{})
        for k,v in r.items():
            if v and not cur.get(k): cur[k]=v.strip()
    pr={}
    for row in csv.reader(open(PROKO_CSV, encoding="utf-8-sig", errors="replace"), delimiter=";"):
        if len(row)<3 or "prokompressor" not in row[1]: continue
        pr[row[1].rstrip("/").split("/")[-1].lower()]=(row[0].strip().replace("&quot;",'"'), row[1].strip())
    def our_reason(name, code, b):
        text=name+" "+code
        if not b: return "бренд не распознан"
        if cat_of(text): return ""    # это категория — учтена в категориях, не дропнута
        if not is_compressor(text): return "не компрессор и не категория (запчасть/масло/аксессуар)"
        if not ser_of(text, b): return "серия+номер не извлекаются"
        return ""
    our=[]
    for code,r in catalog.items():
        name=r.get("IE_NAME","")
        man=(r.get("IP_PROP22553") or "").strip()
        b=brand_from_text(man) or BRAND_ALIASES.get(man.lower().split()[0] if man else "", None) or brand_from_text(name)
        rs=our_reason(name, code, b)
        if not rs: continue            # учтён где-то (компрессор/категория)
        nm,url=pr.get(code.lower(), (name, f"https://prokompressor.ru/catalog/{code}/"))
        our.append([b or "?", nm, man, fmt(sane_kw(num(r.get("IP_PROP22562")))),
                    fmt(bar_value(r.get("IP_PROP22573"))), fmt(flow_value(r.get("IP_PROP22571"),"л/мин")),
                    rs, url])
    HO=["бренд","наш товар","производитель(битрикс)","кВт","бар","произв,л/мин","причина отсева","ссылка"]
    our.sort(key=lambda r:(r[6], r[0]))
    p2=OUTDIR+"/НАШИ_не_вошли_никуда.csv"
    with open(p2,"w",encoding="utf-8-sig",newline="") as fh:
        wr=csv.writer(fh,delimiter=";"); wr.writerow(HO); wr.writerows(our)
    print(f"наши не вошли никуда: {len(our)}")
    print("  причины:", dict(Counter(r[6] for r in our)))
    with zipfile.ZipFile(ZIP,"w",zipfile.ZIP_DEFLATED) as z:
        z.write(p1, os.path.basename(p1)); z.write(p2, os.path.basename(p2))
    print(f"-> {ZIP}")

if __name__=="__main__":
    build()
