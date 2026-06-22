"""Матч новых выгрузок-каталогов (именованные колонки) боевым spec_match.
Наш = prokompressor.ru.csv; конкуренты = ackompressor.ru.csv, engerair.ru.csv.
Адаптер приводит их к тем же диктам товара (sn,kw,bar,fl,vsd,ff,rv,dr,cool), что и
load_ours_all, и гоняет тот же match()+фильтры. Засады данных чиним тут (см. комменты)."""
import csv, sys, os, re, zipfile
csv.field_size_limit(sys.maxsize)
from collections import defaultdict
from matcher import brand_from_text, find_brand, BRAND_ALIASES
from spec_match import (num, sane_kw, bar_value, bar_from_text, flow_value, is_compressor,
                        text_flags, ip_class, cool_class, match, receiver_filter, ff_filter,
                        ip_filter, cool_filter)
from brand_spec_review import ser_of, suffix_flags

U="/root/.claude/uploads/62a19005-a7bf-569b-926e-b59b4a62600d/"
PROKO=U+"426094e6-prokompressor.ru.csv"
ACK  =U+"71590152-ackompressor.ru.csv"
ENG  =U+"a26316af-engerair.ru.csv"
USD_RUB=73.44
OUTDIR="/home/user/Statistic/match_new"; ZIP="/home/user/Statistic/Match_new_files.zip"

_MON={"янв":1,"фев":2,"мар":3,"апр":4,"май":5,"июн":6,"июл":7,"авг":8,"сен":9,"окт":10,"ноя":11,"дек":12}
def fix_excel(v):
    """ackompressor: кВт побит Excel-автодатой: '5.5'->'05.май', '7.5'->'07.июл'? (день.месяц).
    Возвращаем 'день.номер_месяца' = исходное десятичное (05.май -> 5.5)."""
    s=str(v or "").strip().lower()
    m=re.match(r'^(\d{1,2})\.(янв|фев|мар|апр|май|июн|июл|авг|сен|окт|ноя|дек)\.?$', s)
    if m: return f"{int(m.group(1))}.{_MON[m.group(2)]}"
    return v

def flag(v):                       # да/нет -> 1/0/None (тристейт, молчание совместимо)
    s=str(v or "").strip().lower()
    if s in ("да","yes","есть","1"): return 1
    if s in ("нет","no","0"): return 0
    return None

def price_num(v, mult=1.0):
    try:
        x=float(re.sub(r'[^\d.,]','',str(v)).replace(",","."))
        x*=mult
        return x if 100<=x<=50_000_000 else None
    except: return None

def parse_kw(v):
    """engerair: '30+22' (двухмотор PM VSD) — НЕ гадаем, ставим None (молчание совместимо,
    серия+бар+произв решат). Обычные значения — как есть."""
    if v is None: return None
    if "+" in str(v): return None
    return sane_kw(num(v))

# (колонка-источник) под каждый файл; None = поля нет
MAP={
 "proko":dict(vsd="Св-во: CHASTOTNYY_PREOBRAZOVATEL", kw="Св-во: MOSHCHNOST_KVT",
   rvol="Св-во: OBEM_RESIVERA_L", ff="Св-во: OSUSHITEL", flmin="Св-во: PROIZVODITELNOST_L_MIN",
   flm3min="Св-во: PROIZVODITELNOST_M3_MIN", flm3ch="Св-во: PROIZVODITELNOST_M3_CH",
   bar="Св-во: RABOCHEE_DAVLENIE_BAR", rflag="Св-во: RESIVER", cool="Св-во: TIP_OKHLAZHDENIYA",
   dr="Св-во: TIP_PRIVODA", price="Цена: Сайт (RUB)", mult=1.0, kwfix=False, flkey=None),
 "ack":dict(vsd="Св-во: CHASTOTNYY_PREOBRAZOVATEL", kw="Св-во: MOSHCHNOST_DVIGATELYA_KVT",
   rvol="Св-во: OBEM_RESIVERA_L", ff="Св-во: OSUSHITEL", flmin="Св-во: PROIZVODITELNOST_L_MIN",
   flm3min=None, flm3ch=None, bar="Св-во: RABOCHEE_DAVLENIE_BAR", rflag="Св-во: RESIVER",
   cool=None, dr="Св-во: TIP_PRIVODA", price="Цена: Сайт (RUB)", mult=1.0, kwfix=True, flkey=None),
 "eng":dict(vsd="Св-во: CHASTOTNIK", kw="Св-во: MOSHNOST", rvol=None, ff="Св-во: OSUSHITEL",
   flmin="Св-во: PROIZVOD", flm3min=None, flm3ch=None, bar="Св-во: RABDAVL", rflag="Св-во: RESIVER",
   cool="Св-во: TIP_OHLAGDENIYA", dr="Св-во: PRIVOD", price="Цена: Интернет (USD)",
   mult=USD_RUB, kwfix=False, flkey="м3/мин"),   # PROIZVOD у engerair в м³/мин (6 -> 6000)
}

def load(path, kind):
    m=MAP[kind]
    rows=list(csv.DictReader(open(path,encoding="utf-8-sig"),delimiter=";"))
    # группируем по URL. У engerair давление/произв. лежат на РАЗНЫХ sku-строках одного URL
    # (OF-110W: 8 бар и 10 бар) — нельзя сливать в одну, иначе теряется второй вариант.
    tname={}; byurl=defaultdict(list)
    for r in rows:
        u=(r.get("URL") or "").strip(); nm=(r.get("Название") or "").strip()
        if not u or not nm: continue
        byurl[u].append(r)
        if r.get("Тип")=="товар": tname.setdefault(u, nm)   # чистое имя (sku-имена с хвостами)
    def g(r,key): return r.get(m[key]) if m.get(key) else None
    def specced(r): return any(g(r,k) for k in ("kw","bar","flmin","flm3min","flm3ch"))
    out=defaultdict(list); seen=set()
    for u, group in byurl.items():
        nm=tname.get(u) or group[0].get("Название","").strip()
        if not is_compressor(nm): continue
        # имя первично; URL-фолбэк (find_brand) ловит «AC ZR/ZT/GA»=Atlas, но НЕ перебивает бренд
        # токеном «buster» из слага (это и был баг brand_of: enger/dalgakiran -> buster)
        b=brand_from_text(nm) or find_brand(u) or BRAND_ALIASES.get(nm.lower().split()[0] if nm else "", None)
        if not b: continue
        sn=ser_of(nm, b)
        if not sn: continue
        # приоритет строкам с давлением/произв. (реальные вариации); kw-only товар-строку
        # берём только если ничего лучше нет — иначе она даёт рыхлый дубль-матч (бар молчит)
        rich=[x for x in group if g(x,"bar") or g(x,"flmin") or g(x,"flm3min") or g(x,"flm3ch")]
        for r in (rich or [x for x in group if specced(x)] or group[:1]):   # карточка на КАЖДУЮ спек-вариацию
            kw=parse_kw(fix_excel(g(r,"kw")))               # Excel-автодата чинится для любого файла
            bar=bar_value(fix_excel(g(r,"bar"))) or bar_from_text(nm)   # 884 «бар» ackompressor тоже побиты
            fl=None
            if m["flkey"]: fl=flow_value(fix_excel(g(r,"flmin")), m["flkey"])
            else:
                for src,key in (("flmin","л/мин"),("flm3min","м3/мин"),("flm3ch","м3/час")):
                    if g(r,src): fl=flow_value(fix_excel(g(r,src)), key); break
            k=(u, kw, bar, fl)
            if k in seen: continue                          # одинаковые sku не плодим
            seen.add(k)
            ff,vsd,rv=text_flags(nm); ff,rv=suffix_flags(nm, b, ff, rv)
            if vsd is None: vsd=flag(g(r,"vsd"))
            if flag(g(r,"ff"))==1: ff=1
            if rv is None:
                rvol=num(g(r,"rvol")) if g(r,"rvol") else None
                rv=rvol if rvol else (1 if flag(g(r,"rflag"))==1 else None)
            drv=(str(g(r,"dr") or "")).strip().lower() or None
            if drv: drv="ремен" if "ремен" in drv else ("прямой" if "прям" in drv else None)
            out[b].append(dict(sn=sn, kw=kw, bar=bar, fl=fl, vsd=vsd, ff=ff, rv=rv, dr=drv,
                               ip=ip_class(nm), cool=cool_class(nm, g(r,"cool") or ""),
                               name=nm, url=u, price=price_num(g(r,"price"), m["mult"])))
    return out

def fmt(v): return "" if v is None else (f"{v:g}" if isinstance(v,float) else str(v))
def fmtp(v): return "" if v is None else f"{float(v):.0f}"
def spec(o):
    p=[f"{fmt(o[k])}{u}" for k,u in (("kw","кВт"),("bar","бар"),("fl","л/мин")) if o.get(k) is not None]
    if o.get("ff"): p.append("FF")
    if o.get("vsd"): p.append("VSD")
    return " ".join(p)
def cname(c):
    # engerair кладёт все давления на 1 URL с общим именем -> дописываем бар, чтобы строки не выглядели дублями
    return c["name"] + (f" [{c['bar']:g} бар]" if c.get("bar") is not None and f"{c['bar']:g}" not in c["name"] else "")

def run(ours, comp, label):
    rows=[]; matched=set()
    for b in set(ours)|set(comp):
        by=defaultdict(list)
        for c in comp.get(b,[]): by[c["sn"]].append(c)
        for o in ours.get(b,[]):
            mm=receiver_filter(o.get("rv"), ff_filter(o.get("ff"),
                cool_filter(o.get("cool"), ip_filter(o.get("ip"), match(o, by.get(o["sn"],[]))))))
            if mm:
                for c in mm:
                    matched.add(c["url"])
                    rows.append(["есть у обоих", b, o["name"], spec(o), fmtp(o.get("price")),
                                 cname(c), spec(c), fmtp(c.get("price")), o["url"], c["url"]])
            elif b in comp:   # «только у нас» показываем лишь по брендам, которые конкурент ВОЗИТ
                rows.append(["только у нас", b, o["name"], spec(o), fmtp(o.get("price")),"","","",o["url"],""])
    for b,lst in comp.items():
        seen=set()
        for c in lst:
            if c["url"] in matched or c["url"] in seen: continue
            seen.add(c["url"])
            rows.append(["нет у нас (GAP)", b, "","","", cname(c), spec(c), fmtp(c.get("price")),"",c["url"]])
    HEAD=["статус","бренд","наш товар","наши спеки","цена наша,₽","товар конкурента","спеки конкур",
          "цена конкур,₽","наша ссылка","ссылка конкурента"]
    ST={"есть у обоих":0,"только у нас":1,"нет у нас (GAP)":2}
    rows.sort(key=lambda r:(ST[r[0]], r[1], r[2] or r[5]))
    p=os.path.join(OUTDIR, f"match_{label}.csv")
    with open(p,"w",encoding="utf-8-sig",newline="") as fh:
        w=csv.writer(fh,delimiter=";"); w.writerow(HEAD); w.writerows(rows)
    from collections import Counter
    c=Counter(r[0] for r in rows)
    print(f"[{label}] пары: {c['есть у обоих']} | только у нас: {c['только у нас']} | GAP: {c['нет у нас (GAP)']}")
    return p

def main():
    os.makedirs(OUTDIR, exist_ok=True)
    ours=load(PROKO,"proko"); print("наших компрессоров(с серией):", sum(len(v) for v in ours.values()))
    ack =load(ACK,"ack")
    # на ac-kompressor.ru активен ТОЛЬКО Atlas Copco, остальные бренды в файле есть, но не активны
    ack={b:v for b,v in ack.items() if b=="atlas"}
    print("ackompressor АКТИВНЫХ (Atlas Copco):", sum(len(v) for v in ack.values()))
    eng =load(ENG,"eng");     print("engerair:", sum(len(v) for v in eng.values()))
    made=[run(ours,ack,"prokompressor_VS_ackompressor_Atlas"), run(ours,eng,"prokompressor_VS_engerair")]
    with zipfile.ZipFile(ZIP,"w",zipfile.ZIP_DEFLATED) as z:
        for p in made: z.write(p, os.path.basename(p))
    print("->", ZIP)

if __name__=="__main__":
    main()
