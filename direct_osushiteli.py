"""Отдельная раскатка Я.Директ: ОСУШИТЕЛИ (адсорбционные + рефрижераторные) из
prokompressor.ru.csv, без Enger, только с ценой. Схема = direct_full (те же правила
Директа: 7 слов с точкой-разделителем, [точные] без +/-/кавычек, отображаемая ссылка
одиночными дефисами, кампании-чанки <=1000 ГРУПП по цене), но:
- тип-существительное в ключе = «осушитель», прилагательное = адсорбционный/рефрижераторный;
- точка росы «-20C/-40C/-70C» и напряжение «230V» вычищаются из КЛЮЧА (токен с ведущим
  «-» стал бы минус-словом и зарезал показ), в названии группы остаются;
- минус-фраз на группу нет: варианты осушителей различаются хвостами (точка росы, S/HE),
  минусовать голые числа «-40 -70» опасно для обычных запросов."""
import re, os, sys, zipfile
sys.path.insert(0,".")
import direct_full as D          # PRICE, шаблон, clean_model/cap/dlink/wc и правила Директа

OUTDIR="/home/user/Statistic/direct_osushiteli"; ZIP="/home/user/Statistic/Direct_osushiteli.zip"
ITOG="/home/user/Statistic/Direct_osushiteli_ITOG.xlsx"
DRY=re.compile(r"^\s*[\"«]?\s*(адсорбционн\w*|рефрижераторн\w*)?\s*[\"«]?осушитель|"
               r"осушитель\s+(воздуха|рефрижераторн|адсорбционн)", re.I)
TYPE_DRY=re.compile(r"\b(адсорбционн\w*|рефрижераторн\w*|осушитель\w*|воздуха|холодильн\w*|"
                    r"сжатого|горяч\w+\s+регенерац\w+|холодн\w+\s+регенерац\w+|регенерац\w*)\b", re.I)
def dry_clean(s):
    s=D.clean_model(s)                                   # кавычки/тире/напряж(кир.) уже вычищены
    s=re.sub(r"\b\d{3,4}\s?V\b"," ",s,flags=re.I)        # 230V латиницей (clean_model ловит только «В»)
    s=re.sub(r"(?:^|(?<=\s))-\d{1,3}\s?[CС]\b"," ",s)    # точка росы -20C/-40С: в ключе стала бы минус-словом
    s=re.sub(r"\bG\b\s*$"," ",s)                         # хвостовая резьба G
    return re.sub(r"\s+"," ",s).strip(" -")

prods=[]
import csv
for r in csv.DictReader(open(D.SRC,encoding="utf-8-sig"),delimiter=";"):
    nm=(r.get("Название") or "").strip()
    if D.is_compressor(nm) or not DRY.search(nm): continue
    from matcher import brand_from_text, find_brand
    bl=brand_from_text(nm) or find_brand(r.get("URL") or "")
    if bl=="enger": continue
    url=(r.get("URL") or "").strip(); bp=r.get("Цена: Сайт (RUB)") or ""
    price=D.PRICE.get(D.nrm(url)) or (str(int(float(D.num(bp)))) if D.num(bp) else "")
    if not price: continue                               # без цены — не берём
    core=D.clean_model(re.sub(r"\s+"," ",TYPE_DRY.sub(" ",nm)).strip())   # для ГРУППЫ (с точкой росы)
    adj="адсорбционный" if "адсорб" in nm.lower() else ("рефрижераторный" if ("рефриж" in nm.lower() or "холодильн" in nm.lower()) else "")
    prods.append(dict(brand=D.disp(bl), core=core, stem=dry_clean(re.sub(r"\s+"," ",TYPE_DRY.sub(" ",nm)).strip()),
                      url=url, price=price, adj=adj))

def build_phrases(p, broad, inner):
    adj=p["adj"]; tn="осушитель"
    kompr=f"{tn} {broad}"; adjp=f"{adj} {tn} {broad}" if adj else ""
    kupit=f"{broad} купить"; cena=f"{broad} цена"
    hk=D.wc(kompr)<=7; ha=bool(adjp) and D.wc(adjp)<=7; hu=D.wc(kupit)<=7; hc=D.wc(cena)<=7
    bmin=[]
    if hk or ha: bmin.append(f"-{tn}")
    if hu: bmin.append("-купить")
    if hc: bmin.append("-цена")
    out=[(broad+(" "+" ".join(bmin) if bmin else ""),"")]
    if not D.BAD_EXACT.search(inner): out.append((f"[{inner}]",""))
    if hk: out.append((kompr+(" -"+adj if ha else ""),""))
    if ha: out.append((adjp,""))
    if hu: out.append((kupit,""))
    if hc: out.append((cena,""))
    out.append(("---autotargeting","50")); return out

def build():
    os.makedirs(OUTDIR, exist_ok=True)
    for f in os.listdir(OUTDIR): os.remove(os.path.join(OUTDIR,f))
    units=[]
    for p in sorted(prods, key=lambda x:(float(x["price"]), x["core"])):
        broad=D.cap(D.kw_stem(p["stem"]), 7)
        inner=re.sub(r"\s+"," ",re.sub(r"[-/]"," ",broad)).strip()
        h1=D.headline("осушитель", p["stem"])
        txt=D.trimw(f"Надежный поставщик осушителей {p['brand']} — нам доверяют лидеры рынка. Звоните!",81)
        units.append((p, p["core"], h1, txt, D.dlink(broad), "", build_phrases(p,broad,inner)))
    chunks=[]; cur=[]; seen=set(); collapsed=0                  # <=1000 ГРУПП на кампанию
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
        if all(ph.startswith("---") for ph,_ in kept): collapsed+=1
        seen |= {ph for ph,_ in kept if not ph.startswith("---")}
        cur.append((u,kept))
    if cur: chunks.append(cur)
    made=[]; allrows=[]; total=0
    for ci,ch in enumerate(chunks,1):
        lo=int(float(ch[0][0][0]["price"])); hi=int(float(ch[-1][0][0]["price"]))
        camp=f"Осушители {lo}-{hi}"
        rows=[]
        for gnum,((p,grp,h1,txt,dl,gmin,_),kept) in enumerate(ch,1):
            for i,(ph,bid) in enumerate(kept):
                rows.append(D.make_row(p,camp,gnum,ph,bid,h1,txt,dl,grp,gmin,i==0))
        fn=os.path.join(OUTDIR, f"osush_{ci:02d}_{lo}-{hi}.xlsx")
        D._sheet(rows).save(fn); made.append(fn); allrows+=rows; total+=len(rows)
    D._sheet(allrows).save(ITOG)
    with zipfile.ZipFile(ZIP,"w",zipfile.ZIP_DEFLATED) as z:
        for f in made: z.write(f, os.path.basename(f))
    print(f"осушителей: {len(prods)} | кампаний(файлов): {len(made)} | строк: {total} | двойников: {collapsed}")
    print(f"диапазоны: {os.path.basename(made[0])} ... {os.path.basename(made[-1])}")
    print(f"-> {ZIP}\n-> {ITOG}")

if __name__=="__main__":
    build()
