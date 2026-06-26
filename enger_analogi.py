"""Аналоги бренда Enger у конкурентов (6 сайтов) — СПЕК-матчинг (не по названию: у Enger
свои коды серий, на конкурентах их нет). Сцепка строгая: тип (форма винт/поршень + масло)
обязателен, мощность по номиналу, давление ±3%, производительность ±5%, частотник без
явного конфликта (молчание=совместимо). Тип подтверждаем явной формой ИЛИ маслом+произв.
Выход: xlsx, лист «Все аналоги» (строка=пара Энгер↔аналог, со столбцом «почему») +
лист «Сводка по моделям» (число аналогов, мин.цена конкурента, кто дешевле Энгера)."""
import re, sys
sys.path.insert(0,".")
from collections import defaultdict
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from brand_spec_review import load_ours_all, load_comp_all

OUT="/home/user/Statistic/Enger_analogi.xlsx"
NOMINAL=[0.55,0.75,1.1,1.5,2.2,3,4,5.5,7.5,11,15,18.5,22,30,37,45,55,75,90,110,132,160,
         200,250,315,355,400,450,500,560,630,710,800,900,1000,1250,1600]
def nominal(v): return min(NOMINAL, key=lambda x: abs(x-v)) if v else None
def g(v): return "" if v is None else (f"{v:g}" if isinstance(v,float) else str(v))
def yn(v): return "—" if v is None else ("да" if v else "нет")
def disp(b): return "Ingersoll Rand" if b=="ir" else ("Atlas Copco" if b=="atlas" else b.capitalize())

# форма (винт/поршень/спираль/центроб) из названия; у Enger ВД её часто нет -> None,
# тогда тип подтверждаем маслом+производительностью (см. analog_ok).
FORMS=[("винт","винтов"),("поршень","поршнев"),("спираль","спиральн"),("спираль","scroll"),
       ("центроб","центробежн"),("ротор","роторн")]
FORM_DISP={"винт":"винтовой","поршень":"поршневой","спираль":"спиральный",
           "центроб":"центробежный","ротор":"роторный"}
def form_of(name):
    s=(name or "").lower()
    for key,pat in FORMS:
        if pat in s: return key
    return None
def near(a,b,tol): return a is not None and b is not None and abs(a-b)<=tol*max(a,b)
_NOISE=re.compile(r"(?i)\b(винтов\w*|поршнев\w*|спиральн\w*|роторн\w*|центробежн\w*|безмасл\w*|"
                  r"маслян\w*|электрическ\w*|electric|screw|piston|передвижн\w*|дизельн\w*|"
                  r"высокого|низкого|давлени\w*|дожимн\w*|компрессор\w*|kompressor\w*|compressor)\b")
def short_name(name):                                    # для ячейки: бренд+код без типовых слов/скобок
    s=re.sub(r"\([^)]*\)"," ", name or "")               # (IP54)/(с осушителем) — не нужно в ячейке
    s=_NOISE.sub(" ", s); s=re.sub(r"(?i)\bip\s*\d{2}\b"," ",s)
    s=re.sub(r"\s+"," ",s).strip(" -·()")
    return (s[:34].rsplit(" ",1)[0] if len(s)>34 else s) or (name or "")[:34]

def analog_ok(o,c):
    """Строгий аналог. Возврат True/False. Тип ОБЯЗАН быть подтверждён (формой или масло+произв)."""
    if not o.get("kw") or not c.get("kw"): return False
    if nominal(o["kw"])!=nominal(c["kw"]): return False           # мощность по номиналу
    if not o.get("bar") or not c.get("bar"): return False         # давление знаем с обеих
    if not near(o["bar"],c["bar"],0.03): return False             # ±3%
    if o.get("oil") and c.get("oil") and o["oil"]!=c["oil"]: return False   # масло≠безмасло — режем
    if o.get("fl") and c.get("fl") and not near(o["fl"],c["fl"],0.05): return False  # ±5% если оба
    ov,cv=o.get("vsd"),c.get("vsd")
    if ov is not None and cv is not None and ov!=cv: return False  # частотник: явный конфликт
    of,cf=form_of(o["name"]),form_of(c["name"])
    if of and cf:                                                 # форма явна с обеих — должна совпасть
        return of==cf
    # формы нет с одной/двух сторон -> тип подтверждаем маслом + производительностью
    return bool(o.get("oil") and c.get("oil") and o["oil"]==c["oil"]
                and o.get("fl") and c.get("fl") and near(o["fl"],c["fl"],0.05))

def why(o,c):                                                     # «почему аналог» — совпавшее с числами
    p=[f"{g(nominal(o['kw']))} кВт"]
    p.append(f"{g(o['bar'])}≈{g(c['bar'])} бар")
    if o.get("fl") and c.get("fl"): p.append(f"{g(o['fl'])}≈{g(c['fl'])} л/мин")
    of,cf=form_of(o["name"]),form_of(c["name"])
    if of and cf: p.append(FORM_DISP.get(of,of))
    if o.get("oil") and c.get("oil"): p.append(f"{o['oil']}={c['oil']}")
    if o.get("vsd") is not None and c.get("vsd") is not None: p.append(f"частотник {yn(o['vsd'])}={yn(c['vsd'])}")
    return " · ".join(p)

def build():
    ours=load_ours_all(); cands=load_comp_all()
    eng=ours.get("enger",[])
    # дедуп моделей Энгера: одна строка на (серия,кВт,бар,произв,частотник,ресивер); цена — мин.
    emap={}
    for o in eng:
        if not o.get("kw") or not o.get("bar"): continue
        k=(o["sn"], round(o["kw"],1), round(o["bar"],1),
           round(o["fl"]) if o.get("fl") else None, o.get("vsd"), o.get("rv"))
        cur=emap.get(k)
        if cur is None or (o.get("price") and (not cur.get("price") or o["price"]<cur["price"])):
            emap[k]=o
    engmodels=list(emap.values())

    # индекс конкурентов по номиналу мощности (быстрый прунинг), бренд кладём в карточку
    comp_by_kw=defaultdict(list)
    for b,lst in cands.items():
        if b=="enger": continue                                  # сам бренд — не аналог
        for c in lst:
            if c.get("kw"): comp_by_kw[nominal(c["kw"])].append((b,c))

    wide=[]; summary=[]; CT="compressortyt"
    for o in engmodels:
        hits=[(b,c) for (b,c) in comp_by_kw.get(nominal(o["kw"]),[]) if analog_ok(o,c)]
        # группируем предложения в МОДЕЛИ-аналоги: (бренд,серия,кВт,бар,частотник,ресивер)
        groups=defaultdict(list)
        for b,c in hits:
            gk=(b, c["sn"], round(c["kw"]) if c.get("kw") else None,
                round(c["bar"]) if c.get("bar") else None, c.get("vsd"), c.get("rv"))
            groups[gk].append(c)
        analogs=[]
        for gk,offers in groups.items():
            b=gk[0]
            priced=[x for x in offers if x.get("price") and x.get("status")!="снято"]
            ct=[x for x in priced if CT in (x["site"] or "")]         # офферы на compressortyt
            # есть на compressortyt -> показываем ИМЕННО его цену/ссылку (главный конкурент);
            # иначе — самый дешёвый оффер по всем сайтам
            head=(min(ct,key=lambda x:x["price"]) if ct else
                  (min(priced,key=lambda x:x["price"]) if priced else offers[0]))
            name=max(offers,key=lambda x:len(x.get("name") or "")).get("name") or ""
            analogs.append(dict(brand=b, name=name, price=head.get("price"),
                                site=head["site"], url=head["url"], has_ct=bool(ct)))
        if not analogs: continue
        # compressortyt — первыми (главный конкурент), внутри и дальше — по возрастанию цены
        analogs.sort(key=lambda a:(not a["has_ct"], a["price"] is None, a["price"] or 0))
        ofm=form_of(o["name"]); otype=FORM_DISP.get(ofm,"") or (("безмасляный " if o.get("oil")=="безмасло" else "")+ "ВД" if o["bar"]>=20 else (o.get("oil") or ""))
        eprice=o.get("price")
        wide.append(dict(o=o, otype=otype, analogs=analogs))
        # лист 2: сводка по модели
        pr=[a["price"] for a in analogs if a["price"]]; mn=min(pr) if pr else None
        mnb=disp(next(a["brand"] for a in analogs if a["price"]==mn)) if mn else ""
        brands=sorted({disp(a["brand"]) for a in analogs})
        cheaper=("" if (mn is None or eprice is None) else
                 (f"да (-{round((eprice-mn)/eprice*100)}%)" if mn<eprice else "нет"))
        summary.append([o["name"], otype, o.get("kw"), o.get("bar"), o.get("fl"), yn(o.get("vsd")),
                        eprice, len(analogs), len(brands), mn, mnb, cheaper, ", ".join(brands)])

    maxn=_save(wide, summary)
    print(f"моделей Энгера: {len(engmodels)} | с аналогами: {len(wide)} | макс аналогов в строке: {maxn}")
    print(f"-> {OUT}")

H2=["Модель Энгер","Тип","кВт","бар","л/мин","Частотник","Цена Энгер ₽","Аналогов",
    "Брендов","Мин. цена конкур. ₽","Бренд (мин.)","Дешевле Энгера?","Бренды-аналоги"]
def _save(wide, summary):
    from openpyxl.utils import get_column_letter
    wb=openpyxl.Workbook()
    hfill=PatternFill("solid",fgColor="305496"); bold=Font(bold=True,color="FFFFFF")
    ctr=Alignment(horizontal="center",vertical="center",wrap_text=True)
    blue=Font(color="0563C1",underline="single"); red=PatternFill("solid",fgColor="FFC7CE")
    # ЛИСТ 1 «Все аналоги» — широкий: строка=модель Энгера, дальше аналоги по столбцам,
    # цена в ячейке = активная ссылка на страницу аналога; красная заливка = дешевле Энгера.
    ws=wb.active; ws.title="Все аналоги"
    maxn=max((len(w["analogs"]) for w in wide), default=0)
    HDR=["Модель Энгер","Тип","кВт","бар","л/мин","Частотник","Цена Энгер ₽"]+[f"Аналог {i}" for i in range(1,maxn+1)]
    ws.append(HDR)
    for ci in range(1,len(HDR)+1):
        c=ws.cell(1,ci); c.font=bold; c.fill=hfill; c.alignment=ctr
    for w in wide:
        o=w["o"]; r=ws.max_row+1
        for ci,val in enumerate([o["name"],w["otype"],o.get("kw"),o.get("bar"),o.get("fl"),
                                 yn(o.get("vsd")),o.get("price")],1):
            ws.cell(r,ci,val)
        ws.cell(r,7).number_format="# ##0"
        for j,a in enumerate(w["analogs"]):
            cell=ws.cell(r,8+j)
            pf=(f"{int(a['price']):,}".replace(","," ")+" ₽") if a["price"] else "—"
            cell.value=f"{short_name(a['name'])} · {pf}"; cell.font=blue
            if a["url"]: cell.hyperlink=a["url"]                       # активная ссылка с ценой
            if a["price"] and o.get("price") and a["price"]<o["price"]: cell.fill=red
    ws.freeze_panes="H2"                                              # модель+спеки закреплены
    ws.column_dimensions["A"].width=44
    for i in range(8,8+maxn): ws.column_dimensions[get_column_letter(i)].width=26
    # ЛИСТ 2 «Сводка по моделям»
    ws2=wb.create_sheet("Сводка по моделям"); ws2.append(H2)
    for ci in range(1,len(H2)+1):
        c=ws2.cell(1,ci); c.font=bold; c.fill=hfill; c.alignment=ctr
    for row in summary: ws2.append(row)
    for col in (7,10):
        for rr in range(2,ws2.max_row+1):
            cell=ws2.cell(rr,col)
            if isinstance(cell.value,(int,float)): cell.number_format="# ##0"
    ws2.freeze_panes="A2"
    wb.save(OUT)
    return maxn

if __name__=="__main__":
    build()
