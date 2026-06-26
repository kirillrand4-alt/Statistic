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
SAMPLE_BRANDS={"dali","et","atlas","wis","ekomak","kraftmachine"}   # ключи брендов (lowercase)

_BRANDDISP={"atlas":"Atlas Copco","ir":"Ingersoll Rand"}   # полные имена брендов
def disp(b): return "" if not b else _BRANDDISP.get(b, b.capitalize())
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
# срезаем ВСЕ типовые слова (вкл. «поршневой дожимной (бустер)», «высокого давления»,
# «винтовой безмасляный»), оставляя бренд+код. Тип товара для заголовка — отдельно.
TYPE_RE=re.compile(r"\(\s*бустер\s*\)|\b(винтов\w*|поршнев\w*|спиральн\w*|двухступенчат\w*|"
                   r"дизельн\w*|передвижн\w*|роторн\w*|центробежн\w*|безмасл\w*|масл\w*|"
                   r"компрессор\w*|дожимн\w*|бустер|высокого|низкого|давлени\w*)\b", re.I)
def product_type(nm):
    s=nm.lower()
    return "бустер" if ("бустер" in s or "дожимн" in s) else "компрессор"
def headline(typ, model):                           # Заголовок 1: модель ВСЕГДА в заголовке (лимит 56)
    for h in (f"Купить {typ} {model} по спец цене", f"Купить {typ} {model}",
              f"Купить {model} по спец цене", f"Купить {model}"):
        if len(h)<=56: return h
    return trimw(f"Купить {model}",56)              # крайний случай — очень длинная модель
def clean_model(s):                                 # убираем электро-мусор -> короткий ключ (лимит 7 слов)
    s=re.sub(r"ATLAS\s+COPCO","Atlas Copco",s,flags=re.I)   # бренд единообразно
    s=re.sub(r"(?<=\d),(?=\d)", ".", s)             # «7,5» -> «7.5» (иначе рвётся на 2 слова + дубль бара)
    s=re.sub(r"\d{3,4}\s*/\s*[13]\s*/\s*\d{2}(\s*/\s*[A-ZА-Я]{1,3})?"," ",s)  # 400/3/50(/YD)
    s=re.sub(r"\b\d{3,4}\s?[Вв]\b"," ",s)           # напряжение 400В / 6000 В
    s=re.sub(r"\b[13]\s?ф\b"," ",s)                 # фазность 3ф
    s=re.sub(r"\b\d{2}\s?Гц\b"," ",s)               # частота 50 Гц
    s=re.sub(r"без\s*N\s*/?\s*CE"," ",s,flags=re.I)
    s=re.sub(r"\bN\s*/\s*CE\b|/?\s*\bCE\b"," ",s,flags=re.I)
    s=re.sub(r"\(\s*с\s*осушителем\s*\)"," ",s,flags=re.I)
    s=re.sub(r"[/,]"," ",s)
    s=re.sub(r"\b\d{3,4}\s+\d{1,2}(\s+YD)?\b"," ",s,flags=re.I)   # остаток «400 50», «400 3 50 YD»
    s=re.sub(r"\b(380|400|660|690)\b"," ",s)        # остаточное напряжение
    s=re.sub(r"\bYD\b"," ",s,flags=re.I)
    return re.sub(r"\s+"," ",s).strip(" -")
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
    bl=brand_from_text(nm) or find_brand(r.get("URL") or "")
    if bl not in SAMPLE_BRANDS: continue
    b=disp(bl)
    if num(r.get("Св-во: MOSHCHNOST_KVT") or "")!=15.0: continue
    bar=bar_value(r.get("Св-во: RABOCHEE_DAVLENIE_BAR"))
    typ=product_type(nm)
    core=clean_model(re.sub(r"\s+"," ",TYPE_RE.sub(" ",nm)).strip())   # бренд+код, без типовых слов
    url=(r.get("URL") or "").strip(); bp=r.get("Цена: Сайт (RUB)") or ""
    price=PRICE.get(nrm(url)) or (str(int(float(num(bp)))) if num(bp) else "")
    ipm=re.search(r"IP\s?(\d{2})", nm)               # класс защиты из имени
    e=(r.get("Св-во: CHASTOTNYY_PREOBRAZOVATEL") or "").strip().lower()=="да" or \
      bool(re.search(r"(?<![a-zа-яё])VSD(?![a-zа-яё])", nm, re.I))   # частотник: проп или VSD в имени
    prods.append(dict(brand=b, core=core, bar=bar, url=url, price=price, typ=typ,
                      ip=(ipm.group(1) if ipm else None), e=e))

# база модели = core без давления/IP/частотника -> внутри собираем варианты (бар, IP, частотник)
def base_of(p):
    c=p["core"]
    if p["bar"]: c=re.sub(r"[-/ ]?"+re.escape(f"{p['bar']:g}")+r"\b","",c)
    c=re.sub(r"IP\s?\d{2}","",c,flags=re.I)
    c=re.sub(r"(?<![a-zа-яё])(VSD|частотник)(?![a-zа-яё])","",c,flags=re.I)
    return (p["brand"], re.sub(r"[ ,]+"," ",c).strip())
bbar=defaultdict(set); bip=defaultdict(set); be=defaultdict(set)
for p in prods:
    k=base_of(p)
    if p["bar"]: bbar[k].add(p["bar"])
    if p["ip"]: bip[k].add(p["ip"])
    be[k].add(p["e"])

def group_minus(p):     # минус: др. давления + др. IP + (частотник, если у группы его НЕТ)
    k=base_of(p); parts=[]
    for b in sorted(bbar[k]-{p["bar"]}): parts.append(f"-{b:g}")
    for ip in sorted(bip[k]-({p["ip"]} if p["ip"] else set())): parts.append(f"-IP {ip}")
    if (not p["e"]) and (True in be[k]): parts += ["-частотник","-vsd","-инвертор"]
    return " ".join(parts)
def has_std_sibling(p): return False in be[base_of(p)]   # есть ли у модели версия БЕЗ частотника

rows=[]; gnum=0
for p in prods:
    gnum+=1
    grp=p["core"] + (f" {p['bar']:g} бар" if p["bar"] and f"{p['bar']:g}" not in p["core"] else "")
    broad=grp.replace(" бар","").strip()
    inner=re.sub(r"\s+"," ",re.sub(r"[-/]"," ",broad)).strip()
    exact="["+inner+"]"
    h1=headline(p["typ"], p["core"])
    h2="Компрессор Центр"
    txt=trimw(f"Надежный поставщик компрессоров {p['brand']} — нам доверяют лидеры рынка. Звоните!",81)
    disp_link=trimw(re.sub(r"\s+","-",broad),20)
    gmin=group_minus(p)
    # фразы группы: частотная версия (при наличии стандартной) — только по слову частотник/vsd
    if p["e"] and has_std_sibling(p):
        phrases=[(broad+" частотник",""),("["+inner+" частотник]",""),
                 (broad+" vsd",""),("["+inner+" vsd]",""),("---autotargeting","50")]
    else:
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
print(f"товаров(групп): {gnum} | строк: {len(rows)} | баз-моделей: {len(bbar)}")
print(f"-> {OUT}")
# показать группы с минус-фразами
shown=0
for p in prods:
    gm=group_minus(p)
    if gm and shown<4:
        br=p["brand"]; co=p["core"][:34]; ba=p["bar"]
        print("  %-10s %-34s бар %g -> минус-фразы группы: %s" % (br,co,ba,gm)); shown+=1
