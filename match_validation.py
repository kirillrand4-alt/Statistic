"""Валидация сцепок (поэтапно): V1 артикулы, V2 вес, V3 привод, V4 когерентность, V5 взаимность.
Каждый валидатор печатает находки «возможно сцепилось ошибочно» с примерами и пишет лист."""
import re, sys
from collections import defaultdict, Counter
from brand_spec_review import load_ours_all, load_comp_all
from spec_match import match, receiver_filter, ff_filter, ip_filter, cool_filter

def norm_sku(s):
    s=re.sub(r"[^a-zа-я0-9]","",str(s).lower())
    if len(s)>18: return None                     # UUID/внутренние коды compressortyt
    return s if len(s)>=5 and sum(ch.isdigit() for ch in s)>=4 else None

def get_matches():
    ours_all=load_ours_all(); cands_all=load_comp_all()
    out=[]   # (brand, o, [cards])
    for b in set(ours_all)&set(cands_all):
        by=defaultdict(list)
        for c in cands_all[b]: by[c["sn"]].append(c)
        for o in ours_all[b]:
            m=receiver_filter(o.get("rv"), ff_filter(o.get("ff"),
                cool_filter(o.get("cool"), ip_filter(o.get("ip"), match(o, by.get(o["sn"], []))))))
            if m: out.append((b,o,m))
    return out

def v1_sku(matches, show=12):
    bad=[]
    for b,o,cards in matches:
        # заводские артикулы сцепленных карточек (нормализованные, длинные)
        per={}
        for c in cards:
            ns=norm_sku(c.get("sku"))
            if ns: per.setdefault(ns, []).append(c)
        if len(per)>=2:
            # исключаем случай «артикулы разные, потому что это варианты-клоны одного сайта»
            bad.append((b,o,per))
    print(f"V1 АРТИКУЛЫ: строк с ≥2 разными заводскими артикулами среди сцепок: {len(bad)}")
    for b,o,per in bad[:show]:
        print(f"  [{b}] {o['name'][:46]!r}")
        for ns,cs in list(per.items())[:3]:
            print(f"      sku={cs[0].get('sku')!r:<22} [{cs[0]['site']}] {cs[0]['name'][:40]!r}")
    return bad

def v2_weight(matches, tol=0.15, show=12):
    bad=[]
    for b,o,cards in matches:
        if not o.get("we"): continue
        for c in cards:
            if c.get("we") and abs(o["we"]-c["we"])>tol*max(o["we"],c["we"]):
                bad.append((b,o,c))
    print(f"V2 ВЕС: пар наш<->карточка с расхождением веса >{int(tol*100)}%: {len(bad)}")
    for b,o,c in bad[:show]:
        print(f"  [{b}] наш {o['we']:g}кг {o['name'][:40]!r}  <>  {c['we']:g}кг [{c['site']}] {c['name'][:40]!r}")
    return bad

def v3_drive(matches, show=12):
    bad=[]
    for b,o,cards in matches:
        if not o.get("dr"): continue
        for c in cards:
            if c.get("dr") and c["dr"]!=o["dr"]:
                bad.append((b,o,c))
    print(f"V3 ПРИВОД: пар с конфликтом прямой/ременной: {len(bad)}")
    for b,o,c in bad[:show]:
        print(f"  [{b}] наш {o['dr']} {o['name'][:40]!r}  <>  {c['dr']} [{c['site']}] {c['name'][:42]!r}")
    return bad

def v4_coherence(matches, show=10):
    bad=[]
    for b,o,cards in matches:
        full=[c for c in cards if c.get("kw") and c.get("bar") and c.get("fl")]
        for i in range(len(full)):
            for j in range(i+1,len(full)):
                c1,c2=full[i],full[j]
                if (abs(c1["kw"]-c2["kw"])>0.06*max(c1["kw"],c2["kw"]) or
                    abs(c1["bar"]-c2["bar"])>0.10*max(c1["bar"],c2["bar"]) or
                    abs(c1["fl"]-c2["fl"])>0.08*max(c1["fl"],c2["fl"])):
                    bad.append((b,o,c1,c2)); break
            else: continue
            break
    print(f"V4 КОГЕРЕНТНОСТЬ: строк, где сцепленные карточки противоречат ДРУГ ДРУГУ: {len(bad)}")
    for b,o,c1,c2 in bad[:show]:
        print(f"  [{b}] {o['name'][:40]!r}")
        print(f"      [{c1['site']}] kw{c1['kw']}/б{c1['bar']}/{c1['fl']:g} {c1['name'][:36]!r}")
        print(f"      [{c2['site']}] kw{c2['kw']}/б{c2['bar']}/{c2['fl']:g} {c2['name'][:36]!r}")
    return bad

def v5_reciprocity(matches, show=10):
    # карточка с ПОЛНЫМИ спеками, сцепленная с >1 РАЗНЫХ наших товаров = противоречие
    by_url=defaultdict(set); meta={}
    for b,o,cards in matches:
        for c in cards:
            if c.get("kw") and c.get("bar") and c.get("fl"):
                key=re.sub(r"\s+"," ",o["name"].strip().lower())
                by_url[c["url"]].add(key); meta[c["url"]]=(b,c)
    bad=[(u,ks) for u,ks in by_url.items() if len(ks)>1]
    print(f"V5 ВЗАИМНОСТЬ: полных карточек, сцепленных с >1 разным нашим товаром: {len(bad)}")
    for u,ks in bad[:show]:
        b,c=meta[u]
        print(f"  [{b}] {c['name'][:44]!r} -> наши: {sorted(ks)[:2]}")
    return bad

def write_xlsx(m, path="/home/user/Statistic/Validation_review.xlsx"):
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    wb=openpyxl.Workbook(); wb.remove(wb.active)
    blue=Font(color="0563C1", underline="single"); bold=Font(bold=True,color="FFFFFF")
    hf=PatternFill("solid",fgColor="305496"); ce=Alignment(horizontal="center",wrap_text=True)
    def sheet(name, hdr):
        ws=wb.create_sheet(name); ws.append(hdr)
        for i in range(1,len(hdr)+1):
            c=ws.cell(1,i); c.font=bold; c.fill=hf; c.alignment=ce
        return ws
    # V1
    ws=sheet("V1 артикулы", ["№","Бренд","Наш товар","Артикулы (разные!)","Карточки"])
    r=1
    for b,o,per in v1_sku(m, show=0):
        r+=1; ws.cell(r,1,r-1); ws.cell(r,2,b)
        c=ws.cell(r,3,o["name"][:60]); c.hyperlink=o["url"]; c.font=blue
        ws.cell(r,4," | ".join(cs[0].get("sku") or "" for cs in per.values())[:80])
        cards=[cs[0] for cs in per.values()]
        lk=ws.cell(r,5," || ".join(f"[{c2['site'].split('.')[0]}] {c2['name'][:30]}" for c2 in cards[:3]))
        lk.hyperlink=cards[0]["url"]; lk.font=blue
    for i,w in enumerate([5,12,52,46,70],1): ws.column_dimensions[get_column_letter(i)].width=w
    # V2
    ws=sheet("V2 вес", ["№","Бренд","Наш товар","Наш вес","Их вес","Карточка"])
    r=1
    for b,o,c2 in v2_weight(m, show=0):
        r+=1; ws.cell(r,1,r-1); ws.cell(r,2,b)
        cc=ws.cell(r,3,o["name"][:56]); cc.hyperlink=o["url"]; cc.font=blue
        ws.cell(r,4,o["we"]); ws.cell(r,5,c2["we"])
        lk=ws.cell(r,6,f"[{c2['site']}] {c2['name'][:46]}"); lk.hyperlink=c2["url"]; lk.font=blue
    for i,w in enumerate([5,12,52,9,9,60],1): ws.column_dimensions[get_column_letter(i)].width=w
    # V3
    ws=sheet("V3 привод", ["№","Бренд","Наш товар","Наш привод","Их привод","Карточка"])
    r=1
    for b,o,c2 in v3_drive(m, show=0):
        r+=1; ws.cell(r,1,r-1); ws.cell(r,2,b)
        cc=ws.cell(r,3,o["name"][:56]); cc.hyperlink=o["url"]; cc.font=blue
        ws.cell(r,4,o["dr"]); ws.cell(r,5,c2["dr"])
        lk=ws.cell(r,6,f"[{c2['site']}] {c2['name'][:46]}"); lk.hyperlink=c2["url"]; lk.font=blue
    for i,w in enumerate([5,12,52,11,11,60],1): ws.column_dimensions[get_column_letter(i)].width=w
    # V4
    ws=sheet("V4 когерентность", ["№","Бренд","Наш товар","Карточка 1","Карточка 2"])
    r=1
    for b,o,c1,c2 in v4_coherence(m, show=0):
        r+=1; ws.cell(r,1,r-1); ws.cell(r,2,b)
        cc=ws.cell(r,3,o["name"][:54]); cc.hyperlink=o["url"]; cc.font=blue
        l1=ws.cell(r,4,f"[{c1['site'].split('.')[0]}] kw{c1['kw']}/б{c1['bar']}/{c1['fl']:g}")
        l1.hyperlink=c1["url"]; l1.font=blue
        l2=ws.cell(r,5,f"[{c2['site'].split('.')[0]}] kw{c2['kw']}/б{c2['bar']}/{c2['fl']:g}")
        l2.hyperlink=c2["url"]; l2.font=blue
    for i,w in enumerate([5,12,52,36,36],1): ws.column_dimensions[get_column_letter(i)].width=w
    for ws2 in wb:
        ws2.freeze_panes="C2"; ws2.auto_filter.ref=f"A1:{get_column_letter(ws2.max_column)}{ws2.max_row}"
    wb.save(path); print(f"-> {path}")

if __name__=="__main__":
    step=sys.argv[1] if len(sys.argv)>1 else "all"
    m=get_matches()
    print(f"сцепленных наших товаров: {len(m)} | пар: {sum(len(c) for _,_,c in m)}\n")
    if step in ("1","all"): v1_sku(m)
    if step in ("2","all"): print(); v2_weight(m)
    if step in ("3","all"): print(); v3_drive(m)
    if step in ("4","all"): print(); v4_coherence(m)
    if step in ("5","all"): print(); v5_reciprocity(m)
    if step=="xlsx":
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()): write_xlsx(m)
        print("Validation_review.xlsx записан")
