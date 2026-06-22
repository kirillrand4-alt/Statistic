"""Наш каталог из нового prokompressor.ru.csv (именованные колонки) -> по файлу на бренд.
Все товары (компрессоры+осушители+ресиверы+азот+запчасти), бренд из названия (brand_from_text),
нераспознанные -> _не_распознан.csv. Спеки приведены: кВт, бар, произв.->л/мин, ресивер, цена."""
import csv, os, sys, zipfile
csv.field_size_limit(sys.maxsize)
from collections import Counter, defaultdict
from matcher import brand_from_text
from spec_match import num, sane_kw, bar_value, flow_value, is_compressor

SRC="/root/.claude/uploads/62a19005-a7bf-569b-926e-b59b4a62600d/426094e6-prokompressor.ru.csv"
OUTDIR="/home/user/Statistic/nash_po_brendam"; ZIP="/home/user/Statistic/Nash_katalog_po_brendam.zip"
HEAD=["категория","название","кВт","бар","произв,л/мин","ресивер,л","частотник","осушитель",
      "охлаждение","привод","цена,₽","ссылка"]

def disp(b): return "IngersollRand" if b=="ir" else b.capitalize()
def fmt(v): return "" if v is None else (f"{v:g}" if isinstance(v,float) else str(v))
def fmtp(v): return "" if v is None else f"{float(v):.0f}"
def cat(nm):
    s=nm.lower()
    if "осушит" in s: return "осушитель"
    if "азот" in s or "генератор" in s: return "азот"
    if "ресивер" in s or "воздухосборник" in s: return "ресивер"
    if any(w in s for w in ("клапан","фильтр","сепаратор","масло","ремень","ремкомплект","прокладк","шланг","элемент","комплект")): return "запчасть"
    return "компрессор" if is_compressor(nm) else "иное"
def flowlmin(r):
    for c,k in (("Св-во: PROIZVODITELNOST_L_MIN","л/мин"),("Св-во: PROIZVODITELNOST_M3_MIN","м3/мин"),
                ("Св-во: PROIZVODITELNOST_M3_CH","м3/час")):
        if r.get(c):
            v=flow_value(r.get(c),k)
            if v: return v
    return None

def build():
    os.makedirs(OUTDIR, exist_ok=True)
    rows=defaultdict(list); cnt=Counter()
    for r in csv.DictReader(open(SRC,encoding="utf-8-sig"),delimiter=";"):
        nm=(r.get("Название") or "").strip()
        if not nm: continue
        b=brand_from_text(nm) or "_не_распознан"
        cnt[b]+=1
        rows[b].append([cat(nm), nm, fmt(sane_kw(num(r.get("Св-во: MOSHCHNOST_KVT")))),
            fmt(bar_value(r.get("Св-во: RABOCHEE_DAVLENIE_BAR"))), fmtp(flowlmin(r)),
            fmt(num(r.get("Св-во: OBEM_RESIVERA_L"))) if r.get("Св-во: OBEM_RESIVERA_L") else "",
            r.get("Св-во: CHASTOTNYY_PREOBRAZOVATEL") or "", r.get("Св-во: OSUSHITEL") or "",
            r.get("Св-во: TIP_OKHLAZHDENIYA") or "", r.get("Св-во: TIP_PRIVODA") or "",
            fmtp(num(r.get("Цена: Сайт (RUB)")) if r.get("Цена: Сайт (RUB)") else None), r.get("URL") or ""])
    made=[]
    for b in sorted(rows):
        data=sorted(rows[b], key=lambda x:(x[0],x[1]))
        p=os.path.join(OUTDIR, f"{disp(b)}.csv")
        with open(p,"w",encoding="utf-8-sig",newline="") as fh:
            w=csv.writer(fh,delimiter=";"); w.writerow(HEAD); w.writerows(data)
        made.append(p)
    with zipfile.ZipFile(ZIP,"w",zipfile.ZIP_DEFLATED) as z:
        for p in made: z.write(p, os.path.basename(p))
    print(f"Брендов(файлов): {len(made)} | всего товаров: {sum(cnt.values())}")
    for b,n in cnt.most_common(): print(f"  {disp(b):<16}{n}")
    print(f"-> {ZIP}")

if __name__=="__main__":
    build()
