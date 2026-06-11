"""Спек-матч ОСТАЛЬНЫХ товаров: ОСУШИТЕЛИ / РЕСИВЕРЫ / АЗОТ — все уроки компрессорных этапов:
бренд из URL+имени (кириллица, опасные короткие токены), серия+номер обязательно (склейка
дефиса/точки, маркетинг-слова, «+»-поколение), сдвоенные «X/Y», диапазоны-дефис=max,
цена только из price, клоны по физ-ключу на сайте, «не противоречит» только по заполненным,
направленные тип-флаги. 5 листов на категорию + «Проверить карточку» по полю с подтверждением.

Категорийные поля:
  осушители: производительность (л/мин; м3/ч у конкурентов!), давление, ТОЧКА РОСЫ (адсорбц.
             -40/-70 vs рефрижераторные +3/+5 — и тип-флаг адсорб/рефриж ЖЁСТКО)
  ресиверы:  ОБЪЁМ (л, 2%) + давление (10/16/25/40) + исполнение верт/гориз (жёстко, если оба)
  азот:      производительность + ЧИСТОТА % (жёстко: 99.999 != 99.5) + давление"""
import csv, sys, json, re, os, zipfile
csv.field_size_limit(sys.maxsize)
import openpyxl
from openpyxl.utils import get_column_letter
from openpyxl.styles import Font, PatternFill
from collections import defaultdict, Counter
from matcher import brand_of, BRAND_ALIASES, brand_from_text
from spec_match import num, sane_bar, bar_value, flow_value, agree, agree_num
from atlas_need_specs import is_product_url, slug, dm, load_universe
from brand_spec_review import gen_series, _styles, _hdr, COMPETITORS
from scrape_files import U

SPECS_CSV=U+"specs2/specs_compact.csv"; PROKO_CSV=U+"e7171060-products_export_20260608.csv"
OUTDIR="/home/user/Statistic/category_reports"; ZIP="/home/user/Statistic/Categories_spec_match.zip"

# бренды ресиверов/осушителей, которых не было в алиасах
for a,c in {"кондр":"kondr","kondr":"kondr","днт":"dnt","dnt":"dnt","бежецкий":"aso"}.items():
    BRAND_ALIASES.setdefault(a,c)

CATS={
 "osushiteli": dict(title="Осушители",
    rx=re.compile(r'осушител|osushitel|\bdryer', re.I),
    anti=re.compile(r'фильтр|filtr|картридж|сервис|servis|ремкомплект|для\s+осушител', re.I)),
 "resivery": dict(title="Ресиверы",
    rx=re.compile(r'\bресивер|\bresiver|воздухосборник|vozduhosbornik', re.I),
    anti=re.compile(r'фильтр|filtr|клапан|klapan|манометр|прокладк|сервис|для\s+ресивер', re.I)),
 "azot": dict(title="Azot",
    rx=re.compile(r'азот\w*|azot|nitrogen', re.I),
    anti=re.compile(r'фильтр|filtr|картридж|сервис|мембран\w+\s+для', re.I)),
}
def cat_of(text):
    for ck,cfg in CATS.items():
        if cfg["rx"].search(text) and not cfg["anti"].search(text): return ck
    return None

# --- категорийные извлечения ---------------------------------------------------------------
def dew_point(text, d):
    """Точка росы °C: спека 'точка росы' или имя '-40'/'-70 PDP'."""
    for k,v in (d or {}).items():
        if "точк" in k.lower() and "рос" in k.lower():
            m=re.search(r'[-+]?\d+', str(v))
            if m: return float(m.group())
    m=re.search(r'(-\s?[247]0|-\s?25)\b', str(text))
    return float(m.group().replace(" ","")) if m else None

def dryer_type(text):
    t=str(text).lower()
    if "адсорб" in t or "adsorb" in t: return "адсорб"
    if "рефриж" in t or "refrizh" in t or "refrig" in t: return "рефриж"
    return None

def orient(text):
    t=str(text).lower()
    if "вертикал" in t or "vertikal" in t: return "верт"
    if "горизонт" in t or "gorizont" in t: return "гориз"
    return None

def purity(text, d):
    """Чистота азота %: 99.999/99,9/98... из спеков или имени (GN025-98TWL)."""
    for k,v in (d or {}).items():
        if "чистот" in k.lower() or "purity" in k.lower():
            m=re.search(r'\d{2}[.,]?\d*', str(v))
            if m: return float(m.group().replace(",","."))
    m=re.search(r'(9[589](?:[.,]\d+)?)\s*%', str(text))
    if m: return float(m.group(1).replace(",","."))
    m=re.search(r'[- ](9[589](?:[.,]\d+)?)(?:twl|\b)', str(text).lower())
    return float(m.group(1).replace(",",".")) if m else None

def vol_of(text, d):
    """Объём ресивера, л: спека 'объем' или имя «РВ 500/10» (объём/давление)."""
    for k,v in (d or {}).items():
        kl=k.lower()
        if ("объем" in kl or "объём" in kl or "vmest" in kl) and "ресив" not in kl or kl.strip() in ("объем, л","объём, л","объем"):
            n=num(v)
            if n and 5<=n<=50000: return n
    m=re.search(r'[врсc]в?\s?[- ]?(\d{2,5})\s*[/л]', str(text).lower())
    if m and 5<=float(m.group(1))<=50000: return float(m.group(1))
    return None

def ser_cat(text, brand):
    sn=gen_series(text, brand or "")
    if not sn: return None
    # «+»-поколение (CD1,5+ != CD1,5) — урок GA11+
    if re.search(re.escape(str(sn[1]).rstrip('.0'))+r'\s*\+', str(text).replace(",",".")) or "+" in str(text).split(str(int(sn[1])))[-1][:2]:
        return (sn[0]+"+", sn[1])
    return sn

# --- наши ----------------------------------------------------------------------------------
def load_ours_cat():
    rows={}
    for r in csv.DictReader(open(SPECS_CSV, encoding="utf-8-sig", errors="replace"), delimiter=";"):
        code=(r.get("IE_CODE") or "").strip()
        if not code: continue
        cur=rows.setdefault(code,{})
        for k,v in r.items():
            if v and not cur.get(k): cur[k]=v.strip()
    price={}
    for row in csv.reader(open(PROKO_CSV, encoding="utf-8-sig", errors="replace"), delimiter=";"):
        if len(row)<3 or "prokompressor" not in row[1]: continue
        sl=row[1].rstrip("/").split("/")[-1].lower()
        try: p=float(str(row[2]).replace(",",".").replace(" ","")) or None
        except: p=None
        price[sl]=(row[0].strip().replace("&quot;",'"'), row[1].strip(), p)
    ours=defaultdict(lambda: defaultdict(list))   # cat -> brand -> [o]
    for code,r in rows.items():
        name=r.get("IE_NAME","")
        ck=cat_of(name+" "+code)
        if not ck: continue
        man=(r.get("IP_PROP22553") or "").strip()
        b=brand_from_text(man) or BRAND_ALIASES.get(man.lower().split()[0] if man else "", None) \
          or brand_from_text(name)
        if not b: continue
        sn=ser_cat(name+" "+code, b)
        if not sn: continue
        nm,url,p=price.get(code.lower(), (name, f"https://prokompressor.ru/catalog/{code}/", None))
        o=dict(sn=sn, name=nm or name, url=url, price=p,
               bar=sane_bar(num(r.get("IP_PROP22573"))),
               fl=flow_value(r.get("IP_PROP22571"), "л/мин"))
        if ck=="osushiteli":
            o["dp"]=dew_point(name, None); o["typ"]=dryer_type(name+" "+code)
        elif ck=="resivery":
            o["vol"]=num(r.get("IP_PROP22564")) or vol_of(name, None); o["ori"]=orient(name+" "+code)
            o["fl"]=None
        else:
            o["pur"]=purity(name, None)
        ours[ck][b].append(o)
    return ours

# --- конкуренты ------------------------------------------------------------------------------
def load_comp_cat():
    names, specs, _ = load_universe()
    price={}; status={}
    import scrape_files
    for f in scrape_files.SCRAPE_FILES:
        try: fh=open(f, encoding="utf-8-sig", errors="replace")
        except FileNotFoundError: continue
        for r in csv.DictReader(fh):
            u=(r.get("product_url") or "").strip()
            if not u: continue
            try:
                v=float(str(r.get("price","")).replace(",",".").replace(" ",""))
                if 100<=v<=50_000_000: price[u]=v   # санити: артикулы в поле цены (99 млрд) и копейки — мимо
            except: pass
            if "снят" in (r.get("series_status") or "").lower() and "v-p-k.ru/catalog" not in u:
                status[u]="снято"
    cands=defaultdict(lambda: defaultdict(list))
    for u,nm in names.items():
        if dm(u) not in COMPETITORS or not is_product_url(u): continue
        text=(nm or "")+" "+slug(u)
        ck=cat_of(text)
        if not ck or not any(ch.isdigit() for ch in text): continue
        b=brand_of(u, nm)
        if not b: continue
        sn=ser_cat(text, b)
        if not sn: continue
        d=specs.get(u,{})
        bar=None; rfl=None; fk=""
        for k,v in d.items():
            kl=k.lower()
            if bar is None and "давлен" in kl: bar=bar_value(v)
            if rfl is None and ("производ" in kl or "пропускн" in kl): rfl=v; fk=kl
        c=dict(sn=sn, name=nm or slug(u), url=u, site=dm(u), price=price.get(u),
               status=status.get(u,""), bar=bar,
               fl=flow_value(rfl, fk+" "+str(rfl or "")))
        if ck=="osushiteli":
            c["dp"]=dew_point(text, d); c["typ"]=dryer_type(text)
        elif ck=="resivery":
            vol=None
            for k,v in d.items():
                if "объ" in k.lower() and "л" in k.lower():
                    n=num(v)
                    if n and 5<=n<=50000: vol=n; break
            c["vol"]=vol or vol_of(text, None); c["ori"]=orient(text); c["fl"]=None
        else:
            c["pur"]=purity(text, d)
        cands[ck][b].append(c)
    return cands

# --- матч на категорию -----------------------------------------------------------------------
def match_cat(ck, o, cl):
    out=[]
    for c in cl:
        if o["sn"]!=c["sn"]: continue
        if not agree_num(o.get("bar"), c.get("bar"), 0.10): continue
        if ck=="osushiteli":
            if not agree_num(o.get("fl"), c.get("fl"), 0.06): continue
            if o.get("typ") and c.get("typ") and o["typ"]!=c["typ"]: continue
            if o.get("dp") is not None and c.get("dp") is not None and abs(o["dp"]-c["dp"])>5: continue
        elif ck=="resivery":
            if not agree_num(o.get("vol"), c.get("vol"), 0.02): continue
            if o.get("vol") is None or c.get("vol") is None: continue   # объём ОБЯЗАТЕЛЕН с обеих
            if o.get("ori") and c.get("ori") and o["ori"]!=c["ori"]: continue
        else:
            if not agree_num(o.get("fl"), c.get("fl"), 0.06): continue
            if o.get("pur") is not None and c.get("pur") is not None and abs(o["pur"]-c["pur"])>0.05: continue
        out.append(c)
    return out

PKEYF={"osushiteli":("bar","fl","typ","dp"), "resivery":("bar","vol","ori"), "azot":("bar","fl","pur")}
def pkey(ck,c): return (c["sn"],)+tuple(c.get(f) for f in PKEYF[ck])

def card_issue_cat(ck, o, same):
    fields={"osushiteli":[("произв","fl",0.06),("бар","bar",0.10),("т.росы","dp",None)],
            "resivery":[("объём","vol",0.02),("бар","bar",0.10)],
            "azot":[("произв","fl",0.06),("чистота","pur",None),("бар","bar",0.10)]}[ck]
    for label,key,tol in fields:
        ov=o.get(key)
        if ov is None: continue
        others=[(k2,t2) for (l2,k2,t2) in fields if k2!=key]
        def agr(a,b,t): return (abs(a-b)<= (t*max(a,b) if t else 3))
        variant=[c for c in same if c.get(key) is not None
                 and all(o.get(k2) is not None and c.get(k2) is not None and agr(o[k2],c[k2],t2)
                         for k2,t2 in others if o.get(k2) is not None and c.get(k2) is not None)]
        if not variant: continue
        if any(agr(c[key],ov,tol) for c in variant): continue
        for c1 in variant:
            v1=c1[key]; doms={c2["site"] for c2 in variant if agr(c2[key],v1,tol)}
            if len(doms)>=2:
                src=next(c2 for c2 in variant if agr(c2[key],v1,tol))
                return (label,ov,v1,len(doms),src)
    return None

def build():
    os.makedirs(OUTDIR, exist_ok=True)
    ours_all=load_ours_cat(); cands_all=load_comp_cat()
    made=[]
    for ck,cfg in CATS.items():
        st=_styles(); chk=PatternFill("solid", fgColor="FCE4D6")
        wb=openpyxl.Workbook(); wb.remove(wb.active)
        O=ours_all.get(ck,{}); C=cands_all.get(ck,{})
        allours=[(b,o) for b,lst in O.items() for o in lst]
        by=defaultdict(lambda: defaultdict(list))
        for b,lst in C.items():
            for c in lst: by[b][c["sn"]].append(c)
        clean=[]; ambig=[]; n0=0
        for b,o in allours:
            m=match_cat(ck, o, by[b].get(o["sn"],[]))
            per=defaultdict(dict)
            for c in m:
                k=pkey(ck,c); cur=per[c["site"]].get(k)
                if cur is None or (c["price"] and (not cur["price"] or c["price"]<cur["price"])): per[c["site"]][k]=c
            if not per: n0+=1; continue
            (ambig if any(len(v)>1 for v in per.values()) else clean).append((b,o,per))
        SPEC={"osushiteli":lambda x:f"{(x.get('typ') or '')} {x.get('fl') or '—'}л/мин {x.get('bar') or '—'}бар тр{x.get('dp') if x.get('dp') is not None else '—'}",
              "resivery":lambda x:f"{x.get('vol') or '—'}л {x.get('bar') or '—'}бар {(x.get('ori') or '')}",
              "azot":lambda x:f"{x.get('fl') or '—'}л/мин {x.get('pur') or '—'}% {x.get('bar') or '—'}бар"}[ck]
        for tname,rows in (("спек-матч",clean),("неоднозначные",ambig)):
            ws=wb.create_sheet(tname)
            HDR=["№","Бренд","Наш товар","Наши хар-ки","Ваша цена"]+COMPETITORS+["min конк.","Δ %","Проверить карточку"]
            _hdr(ws,HDR,st); r=1
            for b,o,per in sorted(rows,key=lambda t:(-len(t[2]),t[1]["name"])):
                r+=1; ws.cell(r,1,r-1); ws.cell(r,2,b); ws.cell(r,3,o["name"])
                ws.cell(r,4,SPEC(o))
                c5=ws.cell(r,5,o["price"] if o["price"] else "нет цены")
                if o["price"]: c5.number_format="# ##0"
                c5.hyperlink=o["url"]; c5.font=st["blue"]
                prices=[]
                for ci,site in enumerate(COMPETITORS):
                    cell=ws.cell(r,6+ci); cards=list(per.get(site,{}).values())
                    if not cards: cell.fill=st["nomatch"]; continue
                    priced=[c for c in cards if c["price"] and c["status"]!="снято"]
                    show=min(priced,key=lambda c:c["price"]) if priced else cards[0]
                    if show["price"]:
                        cell.value=show["price"]; cell.number_format="# ##0"; cell.hyperlink=show["url"]
                        cell.font=st["strike"] if show["status"]=="снято" else st["blue"]
                        if show["status"]!="снято": prices.append(show["price"])
                    else:
                        cell.value="снято" if show["status"]=="снято" else "По запросу"
                        cell.hyperlink=show["url"]; cell.font=st["strike"] if show["status"]=="снято" else st["blue"]
                    if len(cards)>1: cell.fill=st["warn"]
                if prices:
                    mn=min(prices); ws.cell(r,12,mn).number_format="# ##0"
                    if o["price"]: ws.cell(r,13, round((o["price"]-mn)/mn*100,1))
                iss=card_issue_cat(ck,o,by[b].get(o["sn"],[]))
                if iss:
                    label,ov,v1,nd,src=iss
                    cc=ws.cell(r,14,f"{label}: у нас {ov:g}, у конкур. {v1:g} ({nd} сайт.)")
                    cc.hyperlink=src["url"]; cc.font=st["orange"]; ws.cell(r,3).fill=chk
            widths=[5,11,44,26,11]+[12]*6+[10,8,32]
            for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
            ws.freeze_panes="D2"; ws.auto_filter.ref=f"A1:{get_column_letter(len(HDR))}{r}"
        # GAP
        our_sn={(b,o["sn"]) for b,o in allours}
        groups=defaultdict(lambda: defaultdict(list))
        for b,lst in C.items():
            for c in lst:
                if (b,c["sn"]) in our_sn: continue
                groups[(b,pkey(ck,c))][c["site"]].append(c)
        gap=[(g,s) for g,s in groups.items() if len(s)>=2]
        gap.sort(key=lambda t:-len(t[1]))
        ws=wb.create_sheet("GAP — нет у нас")
        HDR=["№","Бренд","Модель (у конкурентов)","Хар-ки","Сайтов"]+COMPETITORS+["min конк."]
        _hdr(ws,HDR,st); r=1
        for (b,gk),sites in gap:
            r+=1; allc=[c for cs in sites.values() for c in cs]
            ws.cell(r,1,r-1); ws.cell(r,2,b)
            ws.cell(r,3,max(allc,key=lambda c:len(c["name"]))["name"][:65])
            ws.cell(r,4,SPEC(allc[0])); ws.cell(r,5,len(sites)); prices=[]
            for ci,site in enumerate(COMPETITORS):
                cell=ws.cell(r,6+ci); cs=sites.get(site)
                if not cs: cell.fill=st["nomatch"]; continue
                priced=[c for c in cs if c["price"] and c["status"]!="снято"]
                show=min(priced,key=lambda c:c["price"]) if priced else cs[0]
                if show["price"]:
                    cell.value=show["price"]; cell.number_format="# ##0"; cell.hyperlink=show["url"]
                    cell.font=st["strike"] if show["status"]=="снято" else st["blue"]
                    if show["status"]!="снято": prices.append(show["price"])
                else:
                    cell.value="снято" if show["status"]=="снято" else "По запросу"
                    cell.hyperlink=show["url"]; cell.font=st["strike"] if show["status"]=="снято" else st["blue"]
            if prices: ws.cell(r,12,min(prices)).number_format="# ##0"
        widths=[5,11,52,24,7]+[12]*6+[10]
        for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
        ws.freeze_panes="C2"; ws.auto_filter.ref=f"A1:{get_column_letter(len(HDR))}{r}"
        n_gap=r-1
        # Снятые
        ws=wb.create_sheet("Снятые у конкурентов")
        _hdr(ws,["№","Бренд","Карточка (снято)","Сайт","Цена (была)","Хар-ки","У нас (серия)"],st); r=1
        for b,lst in C.items():
            for c in lst:
                if c["status"]!="снято": continue
                r+=1; ws.cell(r,1,r-1); ws.cell(r,2,b)
                nm=ws.cell(r,3,c["name"][:65]); nm.hyperlink=c["url"]; nm.font=st["strike"]
                ws.cell(r,4,c["site"])
                if c["price"]: pc=ws.cell(r,5,c["price"]); pc.number_format="# ##0"; pc.font=st["strike"]
                ws.cell(r,6,SPEC(c))
                if (b,c["sn"]) in our_sn: ws.cell(r,7,"есть")
        widths=[5,11,52,16,11,24,10]
        for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
        n_sny=r-1
        path=os.path.join(OUTDIR, f"{cfg['title']}_spec_review.xlsx"); wb.save(path); made.append(path)
        print(f"{cfg['title']:<10} наших {len(allours):>5} | матч {len(clean):>4} | неодн {len(ambig):>3} | "
              f"без {n0:>4} | GAP {n_gap:>4} | снятых {n_sny:>4}")
    with zipfile.ZipFile(ZIP,"w",zipfile.ZIP_DEFLATED) as z:
        for p in made: z.write(p, os.path.basename(p))
    print(f"-> {ZIP}")

if __name__=="__main__":
    build()
