"""Собирает Excel для проверки: строка = товар prokompressor, в ячейках конкурентов
кликабельная цена (ссылка на их страницу).

Двухуровневое сопоставление:
- ТОЧНОЕ совпадение (тот же набор токенов И тот же порядок) -> обычная ячейка (надёжно).
- ТОТ ЖЕ НАБОР, но другой порядок -> ЖЁЛТАЯ ячейка "проверь" (может быть как безопасная
  перестановка типа 500DR, так и подмена типа Inversys 7 Plus -> решает эксперт).
Дедуп по URL -> мин. цена. Кластеризация по бренду из matcher."""
import openpyxl
from collections import defaultdict
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from matcher import signature, signature_ordered, domain

SRC = "/root/.claude/uploads/62a19005-a7bf-569b-926e-b59b4a62600d/03d65e4a-_______________________.xlsx"
COMPETITORS = ["compressortyt.ru","aerocompressors.ru","pnevmoteh.ru",
               "pnevmo-sklad.ru","v-p-k.ru","rutector.ru"]
DOM_FIX = {"rostov.pnevmo-sklad.ru":"pnevmo-sklad.ru"}

def load_all():
    wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    ws = wb["Лист1"]
    info={}
    for row in ws.iter_rows(min_row=1, values_only=True):
        u=str(row[12]); name=str(row[0])
        try: p=float(str(row[3]).replace(",","."))
        except: p=None
        if u not in info: info[u]=[name,[]]
        if p is not None: info[u][1].append(p)
    return info

def cluster_all(info):
    """brand -> {'ord': ord_key->dom->{url:price}, 'loose': loose_key->dom->{url:price}}."""
    data=defaultdict(lambda: {"ord":defaultdict(lambda:defaultdict(dict)),
                              "loose":defaultdict(lambda:defaultdict(dict))})
    for u,(name,prices) in info.items():
        lk=signature(u); ok=signature_ordered(u); brand=lk.split("::")[0]
        d=domain(u); d=DOM_FIX.get(d,d)
        price=min(prices) if prices else None
        data[brand]["ord"][ok][d][u]=price
        data[brand]["loose"][lk][d][u]=price
    return data

HDR=["№","Отпечаток (модель-ключ)","Название (prokompressor)","Ваша цена","Ваша ссылка"]\
    +COMPETITORS+["ВЕРДИКТ (ок / ошибка: ...)","min конк.","Δ к min, %"]

def _pick_min(d):
    return min(d, key=lambda k:(d[k] is None, d[k]))

def add_sheet(wb, info, data, brand, sheet_name):
    bd=data.get(brand, {"ord":{}, "loose":{}})
    wsx=wb.create_sheet(sheet_name[:31]); wsx.append(HDR)
    blue=Font(color="0563C1", underline="single")
    bold=Font(bold=True, color="FFFFFF")
    hfill=PatternFill("solid", fgColor="305496")
    warn=PatternFill("solid", fgColor="FFE699")     # жёлтый: другой порядок -> проверь
    nomatch=PatternFill("solid", fgColor="F2F2F2")  # серый: не нашлось
    center=Alignment(horizontal="center", vertical="center", wrap_text=True)
    for c in range(1,len(HDR)+1):
        cell=wsx.cell(1,c); cell.font=bold; cell.fill=hfill; cell.alignment=center

    pk_oks=sorted(ok for ok,sites in bd["ord"].items() if "prokompressor.ru" in sites)
    n=0
    for ok in pk_oks:
        pk=bd["ord"][ok]["prokompressor.ru"]
        pk_url=_pick_min(pk); pk_price=pk[pk_url]; pk_name=info[pk_url][0]
        lk=signature(pk_url)
        n+=1; r=n+1
        wsx.cell(r,1,n)
        wsx.cell(r,2,ok.replace("::"," :: "))
        wsx.cell(r,3,pk_name)
        c0=wsx.cell(r,4,pk_price if pk_price else "нет цены")
        if pk_price: c0.number_format="# ##0"
        lkc=wsx.cell(r,5,"открыть"); lkc.hyperlink=pk_url; lkc.font=blue
        exact_prices=[]
        for ci,comp in enumerate(COMPETITORS):
            cell=wsx.cell(r,6+ci)
            exact=bd["ord"].get(ok,{}).get(comp,{})
            loose=bd["loose"].get(lk,{}).get(comp,{})
            if exact:                                   # точный порядок -> надёжно
                cu=_pick_min(exact); pr=exact[cu]
                if pr is not None: cell.value=pr; cell.number_format="# ##0"; exact_prices.append(pr)
                else: cell.value="есть, нет цены"
                cell.hyperlink=cu; cell.font=blue
            elif loose:                                 # другой порядок -> проверь
                cu=_pick_min(loose); pr=loose[cu]
                cell.value=(pr if pr is not None else "есть, нет цены")
                if pr is not None: cell.number_format="# ##0"
                cell.hyperlink=cu; cell.font=blue; cell.fill=warn
            else:
                cell.fill=nomatch
        # col 12 = ВЕРДИКТ (пусто). min/Δ считаем только по НАДЁЖНЫМ (exact) ценам
        if exact_prices:
            mn=min(exact_prices)
            wsx.cell(r,13,mn).number_format="# ##0"
            if pk_price: wsx.cell(r,14, round((pk_price-mn)/mn*100,1))
    widths=[5,40,46,11,8]+[14]*len(COMPETITORS)+[26,11,10]
    for i,w in enumerate(widths,1): wsx.column_dimensions[get_column_letter(i)].width=w
    wsx.freeze_panes="C2"; wsx.auto_filter.ref=f"A1:{get_column_letter(len(HDR))}{n+1}"
    return n

def build_multi(brands, out_path):
    info=load_all(); data=cluster_all(info)
    wb=openpyxl.Workbook(); wb.remove(wb.active)
    for brand, sheet in brands:
        n=add_sheet(wb, info, data, brand, sheet); print(f"  {sheet:<14} {n} товаров")
    wb.save(out_path); print(f"-> {out_path}")

if __name__=="__main__":
    build_multi([("dalgakiran","Dalgakiran")], "/home/user/Statistic/Dalgakiran_review.xlsx")
