"""Собирает Excel для проверки: строка = товар prokompressor, в ячейках конкурентов
кликабельная цена (ссылка на их страницу). Дедуп по URL -> мин. цена."""
import openpyxl, re, sys
from collections import defaultdict
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from matcher import signature, domain

SRC = "/root/.claude/uploads/62a19005-a7bf-569b-926e-b59b4a62600d/03d65e4a-_______________________.xlsx"
COMPETITORS = ["compressortyt.ru","aerocompressors.ru","pnevmoteh.ru",
               "pnevmo-sklad.ru","v-p-k.ru","rutector.ru"]
DOM_FIX = {"rostov.pnevmo-sklad.ru":"pnevmo-sklad.ru"}

def load(brand_filter=None):
    wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    ws = wb["Лист1"]
    # url -> (name, [prices])
    info={}
    for row in ws.iter_rows(min_row=1, values_only=True):
        u=str(row[12])
        if brand_filter and brand_filter not in u.lower(): continue
        name=str(row[0])
        try: p=float(str(row[3]).replace(",","."))
        except: p=None
        if u not in info: info[u]=[name,[]]
        if p is not None: info[u][1].append(p)
    return info

def build(brand_filter, out_path, sheet_name):
    info=load(brand_filter)
    # signature -> domain -> {url: minprice}
    clusters=defaultdict(lambda: defaultdict(dict))
    for u,(name,prices) in info.items():
        d=domain(u); d=DOM_FIX.get(d,d)
        sig=signature(u)
        clusters[sig][d][u]=min(prices) if prices else None

    # rows anchored on prokompressor
    out=openpyxl.Workbook(); wsx=out.active; wsx.title=sheet_name
    hdr=["№","Отпечаток (модель-ключ)","Название (prokompressor)","Ваша цена","Ваша ссылка"]
    hdr+=COMPETITORS+["ВЕРДИКТ (ок / ошибка: ...)","min конк.","Δ к min, %"]
    wsx.append(hdr)

    blue=Font(color="0563C1", underline="single")
    bold=Font(bold=True, color="FFFFFF")
    hfill=PatternFill("solid", fgColor="305496")
    warn=PatternFill("solid", fgColor="FFE699")
    nomatch=PatternFill("solid", fgColor="F2F2F2")
    center=Alignment(horizontal="center", vertical="center", wrap_text=True)
    for c in range(1,len(hdr)+1):
        cell=wsx.cell(1,c); cell.font=bold; cell.fill=hfill; cell.alignment=center

    n=0
    for sig in sorted(clusters):
        if "prokompressor.ru" not in clusters[sig]: continue
        pk=clusters[sig]["prokompressor.ru"]
        # anchor: min price + its url
        pk_url=min(pk, key=lambda k:(pk[k] is None, pk[k]))
        pk_price=pk[pk_url]; pk_name=info[pk_url][0]
        n+=1; r=n+1
        wsx.cell(r,1,n)
        wsx.cell(r,2,sig.replace("::"," :: "))
        wsx.cell(r,3,pk_name)
        cprice=wsx.cell(r,4,pk_price if pk_price else "нет цены")
        if pk_price: cprice.number_format="# ##0"
        link=wsx.cell(r,5,"открыть"); link.hyperlink=pk_url; link.font=blue
        comp_prices=[]
        for ci,comp in enumerate(COMPETITORS):
            col=6+ci
            cell=wsx.cell(r,col)
            if comp in clusters[sig]:
                urls=clusters[sig][comp]
                cu=min(urls, key=lambda k:(urls[k] is None, urls[k]))
                cp=urls[cu]
                ndist=len(urls)
                if cp is not None:
                    cell.value=cp; cell.number_format="# ##0"; comp_prices.append(cp)
                else:
                    cell.value="есть, нет цены"
                cell.hyperlink=cu; cell.font=blue
                if ndist>1:  # неоднозначность: >1 разных товара под один ключ
                    cell.fill=warn
                    cell.comment=None
            else:
                cell.fill=nomatch
        # col 12 = ВЕРДИКТ — оставляем пустым для эксперта
        if comp_prices:
            mn=min(comp_prices)
            wsx.cell(r,13,mn).number_format="# ##0"          # min конк.
            if pk_price:
                wsx.cell(r,14, round((pk_price-mn)/mn*100,1))  # Δ к min, %
    # styling
    widths=[5,34,46,11,8]+[14]*len(COMPETITORS)+[26,11,10]
    for i,w in enumerate(widths,1): wsx.column_dimensions[get_column_letter(i)].width=w
    wsx.freeze_panes="C2"; wsx.auto_filter.ref=f"A1:{get_column_letter(len(hdr))}{n+1}"
    out.save(out_path)
    print(f"{sheet_name}: {n} товаров prokompressor -> {out_path}")

if __name__=="__main__":
    build("hansmann","/home/user/Statistic/Hansmann_review.xlsx","Hansmann")
