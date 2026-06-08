"""Собирает Excel для проверки: строка = товар prokompressor, в ячейках конкурентов
кликабельная цена (ссылка на их страницу). Дедуп по URL -> мин. цена.
Кластеризует по бренду, который определил matcher (а не по подстроке URL)."""
import openpyxl, sys
from collections import defaultdict
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from matcher import signature, domain

SRC = "/root/.claude/uploads/62a19005-a7bf-569b-926e-b59b4a62600d/03d65e4a-_______________________.xlsx"
COMPETITORS = ["compressortyt.ru","aerocompressors.ru","pnevmoteh.ru",
               "pnevmo-sklad.ru","v-p-k.ru","rutector.ru"]
DOM_FIX = {"rostov.pnevmo-sklad.ru":"pnevmo-sklad.ru"}

def load_all():
    """url -> [name, [prices]]  (по всему файлу, один раз)."""
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
    """brand -> signature -> domain -> {url: minprice}."""
    data=defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
    for u,(name,prices) in info.items():
        sig=signature(u); brand=sig.split("::")[0]
        d=domain(u); d=DOM_FIX.get(d,d)
        data[brand][sig][d][u]=min(prices) if prices else None
    return data

HDR=["№","Отпечаток (модель-ключ)","Название (prokompressor)","Ваша цена","Ваша ссылка"]\
    +COMPETITORS+["ВЕРДИКТ (ок / ошибка: ...)","min конк.","Δ к min, %"]

def add_sheet(wb, info, data, brand, sheet_name):
    wsx=wb.create_sheet(sheet_name[:31])
    wsx.append(HDR)
    blue=Font(color="0563C1", underline="single")
    bold=Font(bold=True, color="FFFFFF")
    hfill=PatternFill("solid", fgColor="305496")
    warn=PatternFill("solid", fgColor="FFE699")
    nomatch=PatternFill("solid", fgColor="F2F2F2")
    center=Alignment(horizontal="center", vertical="center", wrap_text=True)
    for c in range(1,len(HDR)+1):
        cell=wsx.cell(1,c); cell.font=bold; cell.fill=hfill; cell.alignment=center

    n=0
    for sig in sorted(data.get(brand, {})):
        sites=data[brand][sig]
        if "prokompressor.ru" not in sites: continue
        pk=sites["prokompressor.ru"]
        pk_url=min(pk, key=lambda k:(pk[k] is None, pk[k]))
        pk_price=pk[pk_url]; pk_name=info[pk_url][0]
        n+=1; r=n+1
        wsx.cell(r,1,n)
        wsx.cell(r,2,sig.replace("::"," :: "))
        wsx.cell(r,3,pk_name)
        cp0=wsx.cell(r,4,pk_price if pk_price else "нет цены")
        if pk_price: cp0.number_format="# ##0"
        lk=wsx.cell(r,5,"открыть"); lk.hyperlink=pk_url; lk.font=blue
        comp_prices=[]
        for ci,comp in enumerate(COMPETITORS):
            cell=wsx.cell(r,6+ci)
            if comp in sites:
                urls=sites[comp]
                cu=min(urls, key=lambda k:(urls[k] is None, urls[k]))
                cprice=urls[cu]; ndist=len(urls)
                if cprice is not None:
                    cell.value=cprice; cell.number_format="# ##0"; comp_prices.append(cprice)
                else:
                    cell.value="есть, нет цены"
                cell.hyperlink=cu; cell.font=blue
                if ndist>1: cell.fill=warn      # неоднозначность: >1 товара под ключ
            else:
                cell.fill=nomatch
        # col 12 = ВЕРДИКТ (пусто для эксперта)
        if comp_prices:
            mn=min(comp_prices)
            wsx.cell(r,13,mn).number_format="# ##0"
            if pk_price:
                wsx.cell(r,14, round((pk_price-mn)/mn*100,1))
    widths=[5,34,46,11,8]+[14]*len(COMPETITORS)+[26,11,10]
    for i,w in enumerate(widths,1): wsx.column_dimensions[get_column_letter(i)].width=w
    wsx.freeze_panes="C2"; wsx.auto_filter.ref=f"A1:{get_column_letter(len(HDR))}{n+1}"
    return n

def build_multi(brands, out_path):
    info=load_all(); data=cluster_all(info)
    wb=openpyxl.Workbook(); wb.remove(wb.active)
    for brand, sheet in brands:
        n=add_sheet(wb, info, data, brand, sheet)
        print(f"  {sheet:<14} {n} товаров")
    wb.save(out_path)
    print(f"-> {out_path}")

if __name__=="__main__":
    NEXT10=[("et","ET"),("zif","ZIF"),("atlas","Atlas"),("fiac","Fiac"),
            ("berg","Berg"),("almig","Almig"),("ariacom","Ariacom"),
            ("kraftmann","Kraftmann"),("remeza","Remeza"),("magnus","Magnus")]
    build_multi(NEXT10, "/home/user/Statistic/Brands_batch2_review.xlsx")
