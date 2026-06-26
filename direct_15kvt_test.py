"""Тестовый генератор кампании Яндекс.Директ «15 квт» по правилам из исходника пользователя.
Фразы: широкая + точная[..] + ---autotargeting (ставка 50 у autotargeting, у остальных пусто).
Заголовок/текст — шаблоны. Минус-фразы на группу[60] — для двойников (др. давления).
Фикс-блоки (быстрые ссылки/уточнения/служебные) копируются из исходного файла-шаблона."""
import csv, sys, re
sys.path.insert(0,".")
from collections import defaultdict
from matcher import brand_from_text, find_brand
from spec_match import is_compressor, num, bar_value

SRC="/root/.claude/uploads/62a19005-a7bf-569b-926e-b59b4a62600d/426094e6-prokompressor.ru.csv"
TPL="/root/.claude/uploads/62a19005-a7bf-569b-926e-b59b4a62600d/6dbcf451-prices_20260626_041449.csv"
NP ="/root/.claude/uploads/62a19005-a7bf-569b-926e-b59b4a62600d/8871701e-prices_20260626_041449.csv"
OUT="/home/user/Statistic/Direct_15kvt_TEST.xlsx"
SAMPLE_BRANDS={"Dali","Et","Atlas","Wis","Ekomak","Kraftmachine"}   # у них есть двойники по давлению

def disp(b): return "" if not b else ("IngersollRand" if b=="ir" else b.capitalize())
def trimw(s,n):                       # обрезка по границе слова/дефиса под лимит
    if len(s)<=n: return s
    cut=s[:n]
    m=max(cut.rfind(" "),cut.rfind("-"))
    return (cut[:m] if m>n*0.6 else cut).rstrip(" -")
def nrm(u): u=(u or "").strip().lower(); return re.sub(r"^https?://","",u).replace("www.","").rstrip("/")
# актуальные цены 26.06 по URL
PRICE={}
for r in csv.DictReader(open(NP,encoding="utf-8-sig")):
    p=(r.get("price") or "").strip()
    if p: PRICE[nrm(r.get("product_url"))]=str(int(float(num(p))))
_TYPE=re.compile(r"^.*?компрессор\s+", re.I)        # «Винтовой безмасляный компрессор » -> срез
def trim(s,n): return s if len(s)<=n else s[:n].rstrip()

# фикс-значения и шапку берём из исходного файла-шаблона
tr=list(csv.reader(open(TPL,encoding="utf-8-sig"),delimiter=";"))
HDR=tr[2]; TEMPLATE=tr[3]; NCOL=len(HDR)
FIXED={0,1,2,3,7,10,11,44,45,50,51,52,59}           # копируем из шаблона как есть
CENA=HDR.index("Цена")

# наши 15-кВт товары выбранных брендов
prods=[]
for r in csv.DictReader(open(SRC,encoding="utf-8-sig"),delimiter=";"):
    nm=(r.get("Название") or "").strip()
    if not is_compressor(nm): continue
    b=disp(brand_from_text(nm) or find_brand(r.get("URL") or ""))
    if b not in SAMPLE_BRANDS: continue
    if num(r.get("Св-во: MOSHCHNOST_KVT") or "")!=15.0: continue
    bar=bar_value(r.get("Св-во: RABOCHEE_DAVLENIE_BAR"))
    core=re.sub(r"\s+"," ",_TYPE.sub("",nm)).strip()    # бренд+код, без типа
    url=(r.get("URL") or "").strip(); bp=r.get("Цена: Сайт (RUB)") or ""
    price=PRICE.get(nrm(url)) or (str(int(float(num(bp)))) if num(bp) else "")
    prods.append(dict(brand=b, core=core, bar=bar, url=url, price=price))

# двойники: база = core без значения давления; внутри базы собираем все давления
def base_of(p):
    c=p["core"]
    if p["bar"]: c=re.sub(r"[-/ ]?"+re.escape(f"{p['bar']:g}")+r"\b","",c)
    return (p["brand"], re.sub(r"\s+"," ",c).strip())
twins=defaultdict(set)
for p in prods:
    if p["bar"]: twins[base_of(p)].add(p["bar"])

def group_minus(p):                                   # минус др. давлений соседей-двойников
    if not p["bar"]: return ""
    others=sorted(twins[base_of(p)]-{p["bar"]})
    return " ".join(f"-{o:g}" for o in others)

rows=[]; gnum=0
for p in prods:
    gnum+=1
    grp=p["core"] + (f" {p['bar']:g} бар" if p["bar"] and f"{p['bar']:g}" not in p["core"] else "")
    broad=grp.replace(" бар","").strip()
    exact="["+re.sub(r"\s+"," ",re.sub(r"[-/]"," ",broad)).strip()+"]"
    h1=trimw(f"Купить компрессор {p['core']} по спец цене",56)
    h2="Компрессор Центр"
    txt=trimw(f"Надежный поставщик компрессоров {p['brand']} — нам доверяют лидеры рынка. Звоните!",81)
    disp_link=trimw(re.sub(r"\s+","-",broad),20)
    gmin=group_minus(p)
    # фразы группы: (фраза, ставка)
    phrases=[(broad,""),(exact,""),("---autotargeting","50")]
    for i,(phrase,bid) in enumerate(phrases):
        row=[""]*NCOL
        for j in FIXED: row[j]=TEMPLATE[j]
        row[0]="-"; row[5]=grp; row[6]=str(gnum)
        row[13]=phrase; row[15]=h1; row[16]=h2; row[17]=txt
        row[40]=p["url"]; row[41]=disp_link; row[46]=bid; row[CENA]=p["price"]
        if i==0: row[60]=gmin                       # минус-фразы на группу — в первой строке группы
        rows.append(row)

import openpyxl
wb=openpyxl.Workbook(); ws=wb.active
ws.append(["Предложение текстовых блоков для кампании"]+[""]*(NCOL-1))
ws.append(HDR)
for r in rows: ws.append(r)
for row in ws.iter_rows():                           # текст -> Excel не считает «-8 -10» и «---autotargeting»
    for c in row: c.number_format="@"
wb.save(OUT)
print(f"товаров(групп): {gnum} | строк: {len(rows)} | двойников-баз: {sum(1 for v in twins.values() if len(v)>1)}")
print(f"-> {OUT}")
# показать группы с минус-фразами
shown=0
for p in prods:
    gm=group_minus(p)
    if gm and shown<4:
        br=p["brand"]; co=p["core"][:34]; ba=p["bar"]
        print("  %-10s %-34s бар %g -> минус-фразы группы: %s" % (br,co,ba,gm)); shown+=1
