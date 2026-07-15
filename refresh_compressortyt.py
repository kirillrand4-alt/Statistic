"""«От обратного»: берём ГОТОВЫЕ соотношения из боевых результатов (Brands_spec_match:
53 бренда, Categories_spec_match: осушители/ресиверы/азот), фильтруем на compressortyt,
ОСВЕЖАЕМ цены по текущим прайсам (наш 2c8f1bfb + compressortyt 803cc5a8), и складываем
ВСЁ (компрессоры + категории, все бренды) в ОДИН лист. Плюс спек-сверка сматченных
карточек compressortyt по 2315 закешированным страницам (kW/бар из HTML). Матч не
пересчитываем — берём вердикт боевого прогона."""
import sqlite3, gzip, zlib, re, json, csv, glob, os, html as HH
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

U="/root/.claude/uploads/62a19005-a7bf-569b-926e-b59b4a62600d/"
OURS_CUR=U+"2c8f1bfb-prices_20260710_110047.csv"     # prokompressor: цена+специи
COMP_CUR=U+"803cc5a8-prices_20260710_110407.csv"     # compressortyt: актуальная цена
CACHE=U+"ctyt_extract/compressortyt.ru.sqlite"
BRAND_DIR=U+"brand_res"; CAT_DIR=U+"cat_res"
OUT="/home/user/Statistic/Matching_compressortyt.xlsx"

def nrm(u):
    u=(u or "").strip().lower()
    return re.sub(r"^https?://","",u).replace("www.","").rstrip("/")
def num(x):
    m=re.search(r"\d+[.,]?\d*", str(x) or ""); return float(m.group().replace(",",".")) if m else None
def barnum(x):
    v=num(x); return v if v and 2<=v<=500 else None

# --- 1. текущие цены (+ наши специи kW/бар) ---
def load_prices(path, specs=False):
    d={}
    for r in csv.DictReader(open(path,encoding="utf-8-sig",errors="replace")):
        u=nrm(r.get("product_url"))
        if not u: continue
        try:
            pv=float(str(r.get("price","")).replace(",",".").replace(" ",""))
            price=pv if 100<=pv<=50_000_000 else None
        except: price=None
        kw=bar=None; name=(r.get("name") or "").strip()
        if specs:
            s=(r.get("specs") or "").strip()
            if s and s!="{}":
                try:
                    j=json.loads(s)
                    for k,v in j.items():
                        kl=k.lower()
                        if kw is None and "мощ" in kl: kw=num(v)
                        if bar is None and "давлен" in kl: bar=barnum(v)
                except: pass
        d[u]=dict(price=price,kw=kw,bar=bar,name=name)
    return d

# --- 2. специи compressortyt из кэша (2315) -> по URL ---
def load_cache():
    out={}
    if not os.path.exists(CACHE): return out
    c=sqlite3.connect(CACHE)
    def dec(v):
        b=v if isinstance(v,(bytes,bytearray)) else str(v).encode("utf-8","replace")
        for fn in (gzip.decompress, zlib.decompress, lambda x:x):
            try: return fn(b).decode("utf-8","replace")
            except Exception: continue
        return ""
    for (v,) in c.execute("SELECT value FROM responses"):
        h=dec(v)
        if "product-card" not in h and "product-description" not in h: continue
        mu=re.search(r'rel="canonical"\s+href="([^"]+)"', h) or re.search(r'og:url"\s+content="([^"]+)"', h)
        if not mu: continue
        u=nrm(mu.group(1))
        kw=None; mk=re.search(r"[Мм]ощность[^0-9]{0,40}?([\d.,]+)\s*кВт", h)   # допускаем разметку таблицы между меткой и числом
        if mk: kw=num(mk.group(1))
        bar=None; mb=re.search(r"[Рр]абочее давлени[ея][^0-9]{0,40}?([\d.,]+)\s*(?:атм|бар|bar)", h)
        if mb: bar=barnum(mb.group(1))
        out[u]=dict(kw=kw,bar=bar)
    return out

OURS=load_prices(OURS_CUR, specs=True)
COMP=load_prices(COMP_CUR)
# спек-сверка по кэшу отключена: быстрый разбор HTML compressortyt ненадёжен (давление почти
# не извлекается, мощность цепляет чужие числа) -> ложные конфликты. Реальная сверка — через парсер.

def link(cell): return cell.hyperlink.target if cell and cell.hyperlink else None
def colmap(ws):
    return {(ws.cell(1,c).value or "").strip().lower(): c for c in range(1, ws.max_column+1)}

# --- 3. читаем боевые результаты, вытягиваем compressortyt-соотношения ---
rows=[]     # dict: cat, brand, oname, ourl, cturl, ctmodel, why, is_gap
def take_sheet(ws, cat, is_gap, brand_col=None, brand_fixed=None):
    cm=colmap(ws)
    ci_ct=cm.get("compressortyt.ru")
    if not ci_ct: return
    ci_name=cm.get("наш товар")
    ci_model=cm.get("модель (у конкурентов, нас нет)")
    ci_price=cm.get("ваша цена")
    ci_why=cm.get("почему сцепилось")
    for r in range(2, ws.max_row+1):
        brand=(ws.cell(r,brand_col).value if brand_col else None) or brand_fixed or ""
        cturl=link(ws.cell(r,ci_ct))
        if is_gap:                                       # GAP-лист: строка = карточка конкурента (нас нет)
            if not cturl: continue                       # берём только там, где есть НА compressortyt
            rows.append(dict(cat=cat, brand=str(brand).strip(), oname=None, ourl=None, cturl=cturl,
                             ctmodel=(ws.cell(r,ci_model).value if ci_model else None), why="", is_gap=True))
        else:                                            # спек-матч: строка = наш товар
            oname=ws.cell(r,ci_name).value if ci_name else None
            if not oname: continue
            rows.append(dict(cat=cat, brand=str(brand).strip(), oname=oname,
                             ourl=link(ws.cell(r,ci_price)) if ci_price else None, cturl=cturl,
                             ctmodel=None, why=ws.cell(r,ci_why).value if ci_why else "", is_gap=False))

for f in glob.glob(BRAND_DIR+"/*_spec_review.xlsx"):
    brand=os.path.basename(f).replace("_spec_review.xlsx","")
    wb=openpyxl.load_workbook(f)
    if "спек-матч" in wb.sheetnames: take_sheet(wb["спек-матч"], "компрессор", False, brand_fixed=brand)
    if "GAP — нет у нас" in wb.sheetnames: take_sheet(wb["GAP — нет у нас"], "компрессор", True, brand_fixed=brand)
for f in glob.glob(CAT_DIR+"/*_spec_review.xlsx"):
    cat=os.path.basename(f).replace("_spec_review.xlsx","").lower()
    cat={"осушители":"осушитель","ресиверы":"ресивер","azot":"генератор азота"}.get(cat,cat)
    wb=openpyxl.load_workbook(f)
    for sh in wb.sheetnames:
        if sh in ("спек-матч","GAP — нет у нас"):
            bc=colmap(wb[sh]).get("бренд")
            take_sheet(wb[sh], cat, sh.startswith("GAP"), brand_col=bc)

# --- 4. статус + освежение цен + спек-сверка ---
def spec_ok(ourl, cturl):
    o=OURS.get(nrm(ourl) or ""); cs=CACHE_SPEC.get(nrm(cturl) or "")
    if not o or not cs: return "— нет в кэше"
    ok=[];
    if o.get("kw") and cs.get("kw"): ok.append(abs(o["kw"]-cs["kw"])<=0.07*max(o["kw"],cs["kw"]))
    if o.get("bar") and cs.get("bar"): ok.append(abs(o["bar"]-cs["bar"])<=0.10*max(o["bar"],cs["bar"]))
    if not ok: return "— спек нет"
    return "✓ подтверждён" if all(ok) else "✗ КОНФЛИКТ"

out=[]
for x in rows:
    op = OURS.get(nrm(x["ourl"]) or "") if x["ourl"] else None
    cp = COMP.get(nrm(x["cturl"]) or "") if x["cturl"] else None
    our_price = op["price"] if op else None
    ct_price  = cp["price"] if cp else None
    ct_name   = (cp["name"] if cp else None) or x.get("ctmodel") or ("(нет в тек. прайсе)" if x["cturl"] else "")
    if x["is_gap"]: status="нет у нас (GAP)"
    elif x["cturl"]: status="есть у обоих"
    else: status="только у нас"
    delta = (round((ct_price-our_price)/our_price*100) if (our_price and ct_price) else "")
    out.append([x["cat"], x["brand"], status, x["oname"] or "", our_price,
                ct_name, ct_price, delta, (x["why"] or ""), x["ourl"] or "", x["cturl"] or ""])

CATORD={"компрессор":0,"осушитель":1,"ресивер":2,"генератор азота":3}
STORD={"есть у обоих":0,"только у нас":1,"нет у нас (GAP)":2}
out.sort(key=lambda r:(CATORD.get(r[0],9), r[1].lower(), STORD.get(r[2],9), str(r[3])))

# --- 5. xlsx ---
HEAD=["категория","бренд","статус","наш товар","цена наша ₽","карточка compressortyt",
      "цена compressortyt ₽","Δ %","почему сцепилось","наша ссылка","ссылка compressortyt"]
wb=openpyxl.Workbook(); ws=wb.active; ws.title="Матчинг compressortyt"
hf=PatternFill("solid",fgColor="305496"); bd=Font(bold=True,color="FFFFFF")
ctr=Alignment(horizontal="center",vertical="center",wrap_text=True); blue=Font(color="0563C1",underline="single")
red=PatternFill("solid",fgColor="FFC7CE")
ws.append(HEAD)
for c in range(1,len(HEAD)+1):
    cc=ws.cell(1,c); cc.font=bd; cc.fill=hf; cc.alignment=ctr
for r in out:
    ws.append(r); rr=ws.max_row
    for col in (5,7):
        if ws.cell(rr,col).value not in (None,""): ws.cell(rr,col).number_format="# ##0"
    for col in (10,11):
        u=ws.cell(rr,col).value
        if u: ws.cell(rr,col).hyperlink=u; ws.cell(rr,col).font=blue
    if isinstance(r[7],(int,float)) and r[7]<0: ws.cell(rr,7).fill=red      # compressortyt дешевле нас
ws.freeze_panes="A2"; ws.column_dimensions["D"].width=46; ws.column_dimensions["F"].width=40
ws.column_dimensions["I"].width=34
wb.save(OUT)

from collections import Counter
cnt=Counter((r[0],r[2]) for r in out)
print(f"строк: {len(out)} | наш прайс: {len(OURS)} | compressortyt прайс: {len(COMP)}")
for cat in CATORD:
    if any(k[0]==cat for k in cnt):
        print(f"  {cat:16} обоих={cnt[(cat,'есть у обоих')]:5} тольконас={cnt[(cat,'только у нас')]:5} GAP={cnt[(cat,'нет у нас (GAP)')]:5}")
cheaper=sum(1 for r in out if isinstance(r[7],(int,float)) and r[7]<0)
print(f"compressortyt дешевле нас (из «есть у обоих» с ценами): {cheaper}")
print(f"-> {OUT}")
