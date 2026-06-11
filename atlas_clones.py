"""Карточки-клоны конкурентов по Atlas, которые СЕЙЧАС не различаются спек-матчем:
один сайт + одинаковое ядро спеков (серия+номер, кВт, бар, произв., FF, VSD, ресивер),
а отличаются ТОЛЬКО фазой / исполнением / охлаждением. Это и есть причина «неоднозначных».
Эти признаки можно добавить в ключ сравнения (как FF/VSD/ресивер) — тогда они разойдутся."""
import re, openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from collections import defaultdict
from atlas_spec_review import load_comp

OUT="/home/user/Statistic/Atlas_clones_review.xlsx"
PHASE=re.compile(r'1\s*ph|3\s*ph|однофазн|тр[еёе]хфазн', re.I)
ENCL =re.compile(r'power\s*pack|pack\s*silenced|pack\s*unsilenc|unsilenc|silenc|trolley|block|'
                 r'\bpack\b|передвижн|на\s*раме|на\s*шасси', re.I)
COOL =re.compile(r'(?<![a-zа-я])(ac|wc)(?![a-zа-я])|водян|возд\w*\s*охл|water\s*cool|air\s*cool', re.I)

def variant(name):
    """Признаки исполнения карточки: (для группировки frozenset, для показа строка)."""
    n=str(name).lower()
    toks=[m.group().strip() for rx in (PHASE,ENCL,COOL) for m in rx.finditer(n)]
    norm=frozenset(t.replace(" ","") for t in toks)
    return norm, " · ".join(dict.fromkeys(toks)) if toks else "—"

def build():
    cands=load_comp()
    groups=defaultdict(list)
    for c in cands:
        groups[(c["site"], c["sn"], c["kw"], c["bar"], c["fl"],
                c["ff"] or 0, c["vsd"] or 0, c["rv"])].append(c)
    # только группы ≥2, где карточки различаются ИМЕННО фазой/исполнением/охлаждением
    cg=[]
    for g in groups.values():
        if len(g)<2: continue
        if len({variant(c["name"])[0] for c in g})>1: cg.append(g)
    cg.sort(key=lambda g:(g[0]["site"], -len(g), g[0]["sn"]))
    print(f"групп (различие = фаза/исполнение/охлаждение): {len(cg)} | карточек {sum(len(g) for g in cg)}")

    wb=openpyxl.Workbook(); ws=wb.active; ws.title="клоны (фаза-исполн-охл)"
    blue=Font(color="0563C1", underline="single")
    strike=Font(color="C00000", underline="single", strike=True)
    bold=Font(bold=True, color="FFFFFF"); hfill=PatternFill("solid", fgColor="305496")
    band=PatternFill("solid", fgColor="EDEDED")
    center=Alignment(horizontal="center", vertical="center", wrap_text=True)
    HDR=["Группа","Сайт","Серия","кВт","бар","произв","FF","VSD","ресивер","Карточек",
         "Признак (фаза/исполн./охл.)","Карточка (имя → ссылка)","Цена","Снято"]
    ws.append(HDR)
    for ci in range(1,len(HDR)+1):
        cell=ws.cell(1,ci); cell.font=bold; cell.fill=hfill; cell.alignment=center
    r=1
    for gi,g in enumerate(cg,1):
        g=sorted(g, key=lambda c:(c["price"] or 1e18))
        for j,c in enumerate(g):
            r+=1
            if gi%2==0:
                for cc in range(1,len(HDR)+1): ws.cell(r,cc).fill=band
            if j==0:
                ws.cell(r,1,gi); ws.cell(r,2,c["site"])
                ws.cell(r,3, f"{c['sn'][0].upper()}{c['sn'][1]:g}")
                ws.cell(r,4,c["kw"]); ws.cell(r,5,c["bar"]); ws.cell(r,6,c["fl"])
                ws.cell(r,7,"FF" if c["ff"] else ""); ws.cell(r,8,"VSD" if c["vsd"] else "")
                ws.cell(r,9,c["rv"] if c["rv"] else ""); ws.cell(r,10,len(g))
            ws.cell(r,11, variant(c["name"])[1])
            nm=ws.cell(r,12, c["name"][:70]); nm.hyperlink=c["url"]
            nm.font=strike if c["status"]=="снято" else blue
            if c["price"]:
                pc=ws.cell(r,13,c["price"]); pc.number_format="# ##0"
            else:
                ws.cell(r,13, "снято" if c["status"]=="снято" else "По запросу")
            if c["status"]=="снято": ws.cell(r,14,"снято").font=strike
    widths=[7,18,8,6,6,9,5,5,9,9,30,72,12,8]
    for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
    ws.freeze_panes="C2"; ws.auto_filter.ref=f"A1:{get_column_letter(len(HDR))}{r}"
    wb.save(OUT); print(f"-> {OUT}")

if __name__=="__main__":
    build()
