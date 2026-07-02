"""Отдельные раскатки Я.Директ: ГЕНЕРАТОРЫ АЗОТА и ГЕНЕРАТОРЫ КИСЛОРОДА из
prokompressor.ru.csv. ВАЖНО: все генераторы в фиде — бренд Enger (203 азот + 12 кислород,
других нет), поэтому правило «без Enger» здесь ОТКЛЮЧЕНО осознанно — иначе кампании пустые.
Схема и правила Директа = direct_full (7 слов с точкой-разделителем, [точные] без
+/-/кавычек, отображаемая ссылка, чанки <=1000 ГРУПП по цене). Тип-существительное в
ключе двухсловное: «генератор азота» / «генератор кислорода». Минус-фраз на группу нет."""
import re, os, sys, zipfile, csv
sys.path.insert(0,".")
import direct_full as D

OUTDIR="/home/user/Statistic/direct_generatory"; ZIP="/home/user/Statistic/Direct_generatory.zip"
ITOG="/home/user/Statistic/Direct_generatory_ITOG.xlsx"
AZ=re.compile(r"генератор\w*\s+азота|азотн\w+\s+(станц|генератор)", re.I)
KI=re.compile(r"генератор\w*\s+кислорода|кислородн\w+\s+(станц|генератор|концентратор)|концентратор\w*\s+кислорода", re.I)
TYPE_GEN=re.compile(r"\b(генератор\w*|азота|кислорода|адсорбционн\w*|мембранн\w*)\b", re.I)

prods=[]
for r in csv.DictReader(open(D.SRC,encoding="utf-8-sig"),delimiter=";"):
    nm=(r.get("Название") or "").strip()
    if D.is_compressor(nm): continue
    cat="азота" if AZ.search(nm) else ("кислорода" if KI.search(nm) else None)
    if not cat: continue
    url=(r.get("URL") or "").strip(); bp=r.get("Цена: Сайт (RUB)") or ""
    price=D.PRICE.get(D.nrm(url)) or (str(int(float(D.num(bp)))) if D.num(bp) else "")
    if not price: continue                               # без цены — не берём
    core=D.clean_model(re.sub(r"\s+"," ",TYPE_GEN.sub(" ",nm)).strip())
    prods.append(dict(brand="Enger", core=core, url=url, price=price, cat=cat))

def build_phrases(p, broad, inner):
    tn=f"генератор {p['cat']}"                           # «генератор азота X» — так и ищут
    kompr=f"{tn} {broad}"; kupit=f"{broad} купить"; cena=f"{broad} цена"
    hk=D.wc(kompr)<=7; hu=D.wc(kupit)<=7; hc=D.wc(cena)<=7
    bmin=[]
    if hk: bmin.append("-генератор")
    if hu: bmin.append("-купить")
    if hc: bmin.append("-цена")
    out=[(broad+(" "+" ".join(bmin) if bmin else ""),"")]
    if not D.BAD_EXACT.search(inner): out.append((f"[{inner}]",""))
    if hk: out.append((kompr,""))
    if hu: out.append((kupit,""))
    if hc: out.append((cena,""))
    out.append(("---autotargeting","50")); return out

def build():
    os.makedirs(OUTDIR, exist_ok=True)
    for f in os.listdir(OUTDIR): os.remove(os.path.join(OUTDIR,f))
    made=[]; allrows=[]; total=0
    for cat,pref,label in (("азота","azot","Генераторы азота"),("кислорода","kislorod","Генераторы кислорода")):
        units=[]
        for p in sorted([x for x in prods if x["cat"]==cat], key=lambda x:(float(x["price"]), x["core"])):
            broad=D.cap(D.kw_stem(p["core"]), 7)
            inner=re.sub(r"\s+"," ",re.sub(r"[-/]"," ",broad)).strip()
            h1=D.headline(f"генератор {cat}", p["core"])
            txt=D.trimw(f"Надежный поставщик генераторов {cat} Enger — нам доверяют лидеры рынка. Звоните!",81)
            units.append((p, p["core"], h1, txt, D.dlink(broad), "", build_phrases(p,broad,inner)))
        chunks=[]; cur=[]; seen=set()                           # <=1000 ГРУПП на кампанию
        def dedup(phr, seen):
            kept=[]
            for ph,bid in phr:
                if ph.startswith("---"): kept.append((ph,bid)); continue
                if ph in seen: continue
                kept.append((ph,bid))
            return kept
        for u in units:
            if len(cur)>=D.MAXGROUPS:
                chunks.append(cur); cur=[]; seen=set()
            kept=dedup(u[6], seen)
            seen |= {ph for ph,_ in kept if not ph.startswith("---")}
            cur.append((u,kept))
        if cur: chunks.append(cur)
        for ci,ch in enumerate(chunks,1):
            lo=int(float(ch[0][0][0]["price"])); hi=int(float(ch[-1][0][0]["price"]))
            camp=f"{label} {lo}-{hi}"
            rws=[]
            for gnum,((p,grp,h1,txt,dl,gmin,_),kept) in enumerate(ch,1):
                for i,(ph,bid) in enumerate(kept):
                    rws.append(D.make_row(p,camp,gnum,ph,bid,h1,txt,dl,grp,gmin,i==0))
            fn=os.path.join(OUTDIR, f"{pref}_{ci:02d}_{lo}-{hi}.xlsx")
            D._sheet(rws).save(fn); made.append(fn); allrows+=rws; total+=len(rws)
        print(f"{label}: товаров {len(units)} | файлов {len(chunks)}")
    D._sheet(allrows).save(ITOG)
    with zipfile.ZipFile(ZIP,"w",zipfile.ZIP_DEFLATED) as z:
        for f in made: z.write(f, os.path.basename(f))
    print(f"итого строк: {total} | файлов: {len(made)}")
    print(f"-> {ZIP}\n-> {ITOG}")

if __name__=="__main__":
    build()
