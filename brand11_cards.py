"""Карточки конкурентов с ценами по брендам, которых НЕТ у нас (вход — список из
competitor_only_brands.py). По файлу на бренд, отсортировано кат->серия->модель->сайт,
чтобы один товар с разных сайтов стоял рядом = фактическое сравнение цен.

Почему не боевой match(): у незнакомых брендов схемы моделей не парсятся (ser_of даёт
None), и строгий спек-матч отсеивает ~всё ещё до сопоставления (GMP/Kaishan/Mikropor -> 0
кросс-сайтовых групп). Поэтому отдаём ВСЕ карточки с ценами, без потери данных."""
import csv, sys, os, zipfile
csv.field_size_limit(sys.maxsize)
from collections import defaultdict, Counter
from atlas_need_specs import load_universe, dm, is_product_url, slug
from matcher import brand_of
from spec_match import num, sane_kw, bar_value, flow_value, is_flow_key, is_compressor
from category_spec_review import cat_of
from brand_spec_review import ser_of
import scrape_files

BRANDS=["gmp","denair","mikropor","brestor","compair","vortex","kaishan","rotair","baldor","friulair","elitech"]
COMP=["compressortyt.ru","aerocompressors.ru","pnevmoteh.ru","pnevmo-sklad.ru","v-p-k.ru","rutector.ru"]
OUTDIR="/home/user/Statistic/brand11_cards"; ZIP="/home/user/Statistic/Бренды11_карточки_цены.zip"
HEAD=["категория","модель","серия","кВт","бар","произв,л/мин","сайт","цена","статус","артикул","ссылка"]

def fmt(v): return "" if v is None else (f"{v:g}" if isinstance(v,float) else str(v))
def fmtp(v): return "" if v is None else f"{float(v):.0f}"

def load_prices():
    price={}; status={}; sku={}
    for f in scrape_files.SCRAPE_FILES:
        try: fh=open(f,encoding="utf-8-sig",errors="replace")
        except FileNotFoundError: continue
        for r in csv.DictReader(fh):
            u=(r.get("product_url") or "").strip()
            if not u: continue
            try:
                v=float(str(r.get("price","")).replace(",",".").replace(" ",""))
                if 100<=v<=50_000_000: price[u]=v
            except: pass
            if "снят" in (r.get("series_status") or "").lower() and "v-p-k.ru/catalog" not in u: status[u]="снято"
            sk=(r.get("sku") or "").strip()
            if sk: sku[u]=sk
    return price, status, sku

def build():
    os.makedirs(OUTDIR, exist_ok=True)
    price,status,sku=load_prices()
    names,specs,_=load_universe()
    rows=defaultdict(list); cnt=Counter(); wp=Counter()
    bset=set(BRANDS)
    for u,nm in names.items():
        if dm(u) not in COMP or not is_product_url(u): continue
        b=brand_of(u,nm)
        if b not in bset: continue
        text=(nm or "")+" "+slug(u)
        cat="компрессор" if is_compressor(text) else (cat_of(text) or "иное")
        sn=ser_of(text,b); d=specs.get(u,{})
        kw=bar=None; rf=fk=None
        for k,v in d.items():
            kl=k.lower()
            if kw is None and "мощ" in kl and "шум" not in kl: kw=sane_kw(num(v))
            if bar is None and "давлен" in kl: bar=bar_value(v)
            if rf is None and is_flow_key(k): rf=v; fk=kl
        fl=flow_value(rf,(fk or "")+" "+str(rf or "")) if rf else None
        cnt[b]+=1; wp[b]+= 1 if price.get(u) else 0
        rows[b].append([cat, nm or slug(u), f"{sn[0]} {sn[1]:g}" if sn else "", fmt(kw),fmt(bar),fmt(fl),
                        dm(u), fmtp(price.get(u)), status.get(u,""), sku.get(u,""), u])
    made=[]
    for b in BRANDS:
        data=sorted(rows[b], key=lambda r:(r[0], r[2], r[1], r[6]))
        p=os.path.join(OUTDIR, f"{b.capitalize()}.csv")
        with open(p,"w",encoding="utf-8-sig",newline="") as fh:
            w=csv.writer(fh,delimiter=";"); w.writerow(HEAD); w.writerows(data)
        made.append(p)
    with zipfile.ZipFile(ZIP,"w",zipfile.ZIP_DEFLATED) as z:
        for p in made: z.write(p, os.path.basename(p))
    print(f'{"бренд":<11}{"карточек":>9}{"с ценой":>9}')
    for b in BRANDS: print(f"{b.capitalize():<11}{cnt[b]:>9}{wp[b]:>9}")
    print(f"-> {ZIP} | {len(made)} файлов")

if __name__=="__main__":
    build()
