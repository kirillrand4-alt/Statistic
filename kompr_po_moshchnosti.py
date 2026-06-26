"""Компрессоры (без Enger) из prokompressor.ru.csv -> по файлу на НОМИНАЛьную мощность
(15.1/14.8/16.1 -> файл «15»: округление к ближайшему стандарту R-ряда). Бренд первым
столбцом (atlas->Atlas Copco), цена обновлена из прайса 26.06, xlsx с текст-ячейками."""
import csv, os, sys, zipfile
csv.field_size_limit(sys.maxsize)
from collections import defaultdict, Counter
import openpyxl
from matcher import brand_from_text, find_brand
from spec_match import is_compressor, num

SRC="/root/.claude/uploads/62a19005-a7bf-569b-926e-b59b4a62600d/426094e6-prokompressor.ru.csv"
NP ="/root/.claude/uploads/62a19005-a7bf-569b-926e-b59b4a62600d/8871701e-prices_20260626_041449.csv"
OUTDIR="/home/user/Statistic/kompr_po_moshchnosti"; ZIP="/home/user/Statistic/Kompressory_po_moshchnosti.zip"
PRICECOL="Цена: Сайт (RUB)"
NOMINAL=[0.55,0.75,1.1,1.5,2.2,3,4,5.5,7.5,11,15,18.5,22,30,37,45,55,75,90,110,132,160,
         200,250,315,355,400,450,500,560,630,710,800,900,1000,1250,1600]
_BD={"atlas":"Atlas Copco","ir":"Ingersoll Rand"}
def disp(b): return "" if not b else _BD.get(b, b.capitalize())
def nrm(u):
    import re; u=(u or "").strip().lower(); return re.sub(r"^https?://","",u).replace("www.","").rstrip("/")
def nominal(v): return min(NOMINAL, key=lambda x: abs(x-v))   # ближайший номинал

def build():
    os.makedirs(OUTDIR, exist_ok=True)
    price={}
    for r in csv.DictReader(open(NP,encoding="utf-8-sig")):
        p=(r.get("price") or "").strip()
        if p: price[nrm(r.get("product_url"))]=str(int(float(num(p))))
    rd=csv.DictReader(open(SRC,encoding="utf-8-sig"),delimiter=";")
    fields=["бренд"]+rd.fieldnames+["цена: статус"]
    groups=defaultdict(list); st=Counter()
    for r in rd:
        nm=(r.get("Название") or "").strip()
        if not is_compressor(nm): continue
        bl=brand_from_text(nm) or find_brand(r.get("URL") or "")
        if bl=="enger": continue
        v=num(r.get("Св-во: MOSHCHNOST_KVT") or "")
        if not v: key="_без_мощности"
        else: key=f"{nominal(v):g}кВт"
        newp=price.get(nrm(r.get("URL") or ""))
        if newp: r[PRICECOL]=newp; status="обновлена 26.06"; st["обновлена"]+=1
        elif (r.get(PRICECOL) or "").strip(): status="старая (Битрикс)"; st["старая"]+=1
        else: status="нет цены"; st["нет"]+=1
        groups[key].append({"бренд":disp(bl), **r, "цена: статус":status})
    def sk(k): return (k=="_без_мощности", float(k.replace("кВт","")) if k!="_без_мощности" else 0)
    made=[]
    for key in sorted(groups,key=sk):
        wb=openpyxl.Workbook(); ws=wb.active; ws.append(fields)
        for r in groups[key]: ws.append([("" if r.get(f) is None else str(r.get(f))) for f in fields])
        for row in ws.iter_rows():
            for c in row: c.number_format="@"
        p=os.path.join(OUTDIR, key.replace("/","_")+".xlsx"); wb.save(p); made.append(p)
    with zipfile.ZipFile(ZIP,"w",zipfile.ZIP_DEFLATED) as z:
        for p in made: z.write(p, os.path.basename(p))
    print(f"файлов(номиналов): {len(made)} | товаров: {sum(len(v) for v in groups.values())} | цена: {dict(st)}")
    print("номиналы:", [os.path.basename(p).replace('.xlsx','') for p in made])
    print(f"-> {ZIP}")

if __name__=="__main__":
    build()
