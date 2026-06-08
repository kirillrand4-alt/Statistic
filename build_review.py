"""Собирает Excel для проверки: строка = товар prokompressor, в ячейках конкурентов
кликабельная цена (ссылка на их страницу).

Сопоставление по УМНОМУ отпечатку (matcher.signature): безопасные перестановки
склеиваются автоматически, структурные различия расходятся. Дедуп по URL -> мин. цена.
ЖЁЛТЫЙ = под один ключ у конкурента попало >1 разного URL (возможная неоднозначность)."""
import openpyxl, re
from collections import defaultdict
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from matcher import signature, domain

# Детектор «дробь в названии разная» (класс 5.5↔55, который URL не различает).
# Фильтруем Excel-битьё названий (1.46031 = сожранное 1,9/1,0) — там матч по URL верный.
_DEC=re.compile(r'\d+[.,]\d{1,2}(?!\d)')
_CORRUPT=re.compile(r'\d+[.,]\d{3,}')
def _clean_dec(name):
    return frozenset(m.group().replace(",",".")
                     for m in _DEC.finditer(re.sub(r'(?i)ip\s*\d+'," ",str(name))))
def _corrupt(name): return bool(_CORRUPT.search(str(name)))

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
    """brand -> key -> domain -> {url: price}."""
    data=defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
    for u,(name,prices) in info.items():
        key=signature(u); brand=key.split("::")[0]
        d=domain(u); d=DOM_FIX.get(d,d)
        data[brand][key][d][u]=min(prices) if prices else None
    return data

HDR=["№","Отпечаток","Название","У нас","Конк-тов","Ваша цена","Ваша ссылка"]\
    +COMPETITORS+["min конк.","Δ к min, %","ВЕРДИКТ (ок / ошибка: ...)"]

def _pick_min(d):
    return min(d, key=lambda k:(d[k] is None, d[k]))

def _show_key(key):
    p=key.split("::")
    parts=[p[0]] + [x for x in p[1:] if x]
    return " · ".join(parts)

def _best_name(info, sites):
    """Лучшее имя среди конкурентов: длинное и без Excel-битья (.46xxx)."""
    cand=[u for c in COMPETITORS if c in sites for u in sites[c]]
    return info[max(cand, key=lambda u: len(info[u][0]) - (60 if ".46" in info[u][0] else 0))][0]

def add_sheet(wb, info, data, brand, sheet_name):
    bd=data.get(brand, {})
    wsx=wb.create_sheet(sheet_name[:31]); wsx.append(HDR)
    blue=Font(color="0563C1", underline="single")
    bold=Font(bold=True, color="FFFFFF")
    hfill=PatternFill("solid", fgColor="305496")
    warn=PatternFill("solid", fgColor="FFE699")     # жёлтый: >1 разный URL у конкурента
    nomatch=PatternFill("solid", fgColor="F2F2F2")  # серый: не нашлось
    gapfill=PatternFill("solid", fgColor="DDEBF7")  # голубой: кандидат добавить (нас нет)
    center=Alignment(horizontal="center", vertical="center", wrap_text=True)
    for c in range(1,len(HDR)+1):
        cell=wsx.cell(1,c); cell.font=bold; cell.fill=hfill; cell.alignment=center

    ncomp=lambda s: sum(1 for c in COMPETITORS if c in s)
    pk_keys=sorted(k for k,s in bd.items() if "prokompressor.ru" in s)
    gap_keys=sorted((k for k,s in bd.items()
                     if "prokompressor.ru" not in s and ncomp(s)>=2),
                    key=lambda k:(-ncomp(bd[k]), k))      # популярные — первыми
    n=0
    for key in pk_keys+gap_keys:
        sites=bd[key]; has_pk="prokompressor.ru" in sites; nc=ncomp(sites)
        if has_pk:
            pk=sites["prokompressor.ru"]; pk_url=_pick_min(pk)
            pk_price=pk[pk_url]; name=info[pk_url][0]
        else:
            pk_url=None; pk_price=None; name=_best_name(info, sites)
        adec=_clean_dec(name); acorr=_corrupt(name)
        n+=1; r=n+1
        wsx.cell(r,1,n)
        wsx.cell(r,2,_show_key(key))
        wsx.cell(r,3,name)
        uc=wsx.cell(r,4,"да" if has_pk else "нет")
        if not has_pk: uc.fill=gapfill
        cc=wsx.cell(r,5,nc)
        if not has_pk and nc>=4: cc.font=Font(bold=True)   # очень популярный кандидат
        if has_pk:
            c6=wsx.cell(r,6, pk_price if pk_price else "нет цены")
            if pk_price: c6.number_format="# ##0"
            l=wsx.cell(r,7,"открыть"); l.hyperlink=pk_url; l.font=blue
        else:
            wsx.cell(r,6,"—").fill=gapfill
        comp_prices=[]
        for ci,comp in enumerate(COMPETITORS):
            cell=wsx.cell(r,8+ci)
            urls=sites.get(comp)
            if urls:
                cu=_pick_min(urls); pr=urls[cu]
                if pr is not None: cell.value=pr; cell.number_format="# ##0"; comp_prices.append(pr)
                else: cell.value="есть, нет цены"
                cell.hyperlink=cu; cell.font=blue
                dec_bad=(has_pk and not acorr and not _corrupt(info[cu][0])
                         and _clean_dec(info[cu][0])!=adec)   # риск занижения — только наши строки
                if len(urls)>1 or dec_bad: cell.fill=warn   # неоднозначность / разная дробь
            else:
                cell.fill=nomatch
        if comp_prices:
            mn=min(comp_prices)
            wsx.cell(r,14,mn).number_format="# ##0"
            if pk_price: wsx.cell(r,15, round((pk_price-mn)/mn*100,1))
    widths=[5,40,44,7,9,11,8]+[13]*len(COMPETITORS)+[11,10,24]
    for i,w in enumerate(widths,1): wsx.column_dimensions[get_column_letter(i)].width=w
    wsx.freeze_panes="D2"; wsx.auto_filter.ref=f"A1:{get_column_letter(len(HDR))}{n+1}"
    return n

def build_multi(brands, out_path):
    info=load_all(); data=cluster_all(info)
    wb=openpyxl.Workbook(); wb.remove(wb.active)
    for brand, sheet in brands:
        n=add_sheet(wb, info, data, brand, sheet); print(f"  {sheet:<14} {n} товаров")
    wb.save(out_path); print(f"-> {out_path}")

if __name__=="__main__":
    build_multi([("dalgakiran","Dalgakiran")], "/home/user/Statistic/Dalgakiran_review.xlsx")
