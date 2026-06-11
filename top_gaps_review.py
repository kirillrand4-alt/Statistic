"""Топ-бренды: «дополнительные данные» по компрессорам — то, что у конкурентов есть, а у нас
нет (gap: ≥2 конкурента), и снятые с производства. Только компрессоры (is_compressor).
Источник цен/статусов/имён — build_review.load_all; кластеризация — signature."""
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from urllib.parse import urlparse, unquote
from build_review import load_all, cluster_all, COMPETITORS, STATUS, _pick_min, _show_key, _best_name
from spec_match import is_compressor

OUT="/home/user/Statistic/Top_gaps_review.xlsx"
BRANDS=[("ceccato","Ceccato"),("fini","Fini"),("ekomak","Ekomak")]

def _slug(u):
    p=unquote(urlparse(str(u)).path).rstrip("/"); return p.split("/")[-1] if p else ""

def _is_comp_cluster(info, sites):
    """Кластер — компрессор, если имя+слаг любого URL проходят is_compressor."""
    for c in list(COMPETITORS)+["prokompressor.ru"]:
        for u in sites.get(c,{}):
            if is_compressor((info[u][0] or "")+" "+_slug(u)): return True
    return False

HDR=["№","Статус","Отпечаток","Название","У нас","Конк-тов","Ваша цена","Ваша ссылка"]\
    +COMPETITORS+["min конк.","снято у (сайтов)"]

def add_sheet(wb, info, data, brand, title):
    bd=data.get(brand, {})
    ws=wb.create_sheet(title[:31]); ws.append(HDR)
    blue=Font(color="0563C1", underline="single")
    strike=Font(color="C00000", underline="single", strike=True)
    bold=Font(bold=True, color="FFFFFF"); hfill=PatternFill("solid", fgColor="305496")
    warn=PatternFill("solid", fgColor="FFE699")     # >1 URL у конкурента
    nomatch=PatternFill("solid", fgColor="F2F2F2")
    gapfill=PatternFill("solid", fgColor="DDEBF7")  # голубой: нас нет
    checkfill=PatternFill("solid", fgColor="C6E0B4")
    snyfill=PatternFill("solid", fgColor="FCE4D6")  # бледно-оранжевый: есть снятые
    center=Alignment(horizontal="center", vertical="center", wrap_text=True)
    for c in range(1,len(HDR)+1):
        cell=ws.cell(1,c); cell.font=bold; cell.fill=hfill; cell.alignment=center
    ncomp=lambda s: sum(1 for c in COMPETITORS if c in s)
    # только компрессоры; gap = нас нет и ≥2 конкурента; снятые — отдельно считаем
    keys=[k for k,s in bd.items() if _is_comp_cluster(info,s)]
    pk=sorted(k for k in keys if "prokompressor.ru" in bd[k])
    gap=sorted((k for k in keys if "prokompressor.ru" not in bd[k] and ncomp(bd[k])>=2),
               key=lambda k:(-ncomp(bd[k]), k))
    n=0
    for kind,key in [("наш",k) for k in pk]+[("GAP",k) for k in gap]:
        sites=bd[key]; has_pk="prokompressor.ru" in sites; nc=ncomp(sites)
        # сколько сайтов сняли товар с производства
        sny=sum(1 for c in COMPETITORS if c in sites for u in sites[c] if STATUS.get(u)=="снято")
        # для «наших» без gap и без снятых доп.данных нет — пропускаем (нужны только доп-данные)
        if kind=="наш" and nc==0: continue
        if has_pk:
            pkd=sites["prokompressor.ru"]; pk_url=_pick_min(pkd); pk_price=pkd[pk_url]; name=info[pk_url][0]
        else:
            pk_url=None; pk_price=None; name=_best_name(info, sites)
        n+=1; r=n+1
        ws.cell(r,1,n)
        st = "GAP — нет у нас" if kind=="GAP" else ("снято у конкур." if sny else "наш+конк")
        sc=ws.cell(r,2,st)
        ws.cell(r,3,_show_key(key)); ws.cell(r,4,name)
        uc=ws.cell(r,5,"да" if has_pk else "нет")
        if not has_pk: uc.fill=gapfill
        cc=ws.cell(r,6,nc)
        if not has_pk and nc>=4: cc.font=Font(bold=True)
        if has_pk:                                   # наш товар — ссылка ВСЕГДА, даже без цены
            c7=ws.cell(r,7, pk_price if pk_price else "нет цены")
            if pk_price: c7.number_format="# ##0"
            l=ws.cell(r,8,"открыть"); l.hyperlink=pk_url; l.font=blue
        else:
            ws.cell(r,7,"—").fill=gapfill
        comp_prices=[]
        for ci,comp in enumerate(COMPETITORS):
            cell=ws.cell(r,9+ci); urls=sites.get(comp)
            if not urls: cell.fill=nomatch; continue
            priced={u:p for u,p in urls.items() if p is not None}
            if priced:
                cu=_pick_min(priced); pr=priced[cu]
                cell.value=pr; cell.number_format="# ##0"; cell.hyperlink=cu
                if STATUS.get(cu)=="снято": cell.font=strike
                else:
                    cell.font=blue; comp_prices.append(pr)
                    if len(urls)>1: cell.fill=warn
            else:
                cu=next(iter(urls))
                if STATUS.get(cu)=="снято":
                    cell.value="снято"; cell.hyperlink=cu; cell.font=strike
                else:
                    cell.value="проверить"; cell.hyperlink=cu; cell.font=blue; cell.fill=checkfill
        if comp_prices: ws.cell(r,15,min(comp_prices)).number_format="# ##0"
        if sny:
            sc.fill=snyfill; ws.cell(r,16,sny)
    widths=[5,16,34,42,6,8,11,9]+[12]*len(COMPETITORS)+[11,14]
    for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
    ws.freeze_panes="E2"; ws.auto_filter.ref=f"A1:{get_column_letter(len(HDR))}{n+1}"
    g=sum(1 for k in gap); s=sum(1 for k in pk+gap
        if any(STATUS.get(u)=="снято" for c in COMPETITORS if c in bd[k] for u in bd[k][c]))
    print(f"  {title:<10} строк {n} | GAP(нет у нас, ≥2 конк.) {g} | со снятыми {s}")

def build():
    info=load_all(); data=cluster_all(info)
    wb=openpyxl.Workbook(); wb.remove(wb.active)
    for brand,title in BRANDS: add_sheet(wb, info, data, brand, title)
    wb.save(OUT); print(f"-> {OUT}")

if __name__=="__main__":
    build()
