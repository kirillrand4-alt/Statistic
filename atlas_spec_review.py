"""Спек-матч Atlas Copco: наш каталог (specs_compact, Битрикс) x конкуренты (specs из
прогонов парсера). Сцепка — spec_match.match: серия+номер обязательно, кВт/бар/произв-ть
не противоречат, FF/VSD/ресивер из текста жёстко. 1 кандидат -> «1-в-1», 2-3 -> «2-3 канд.»,
>3 — не матчим (в Excel не выводим). Формат: строка = наш товар, кандидаты в колонках."""
import csv, sys, json, re
csv.field_size_limit(sys.maxsize)
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from collections import defaultdict
from matcher import brand_of, domain
from spec_match import (num, sane_kw, sane_bar, bar_from_text, flow_value, series_num,
                        text_flags, is_compressor, match, receiver_filter)
from atlas_need_specs import is_product_url, slug, dm, best_name
COMPETITORS = ["compressortyt.ru","aerocompressors.ru","pnevmoteh.ru",
               "pnevmo-sklad.ru","v-p-k.ru","rutector.ru"]   # порядок колонок как в осн. отчётах
from scrape_files import SCRAPE_FILES, U

SPECS_CSV = U + "specs2/specs_compact.csv"
PROKO_CSV = U + "e7171060-products_export_20260608.csv"
OUT = "/home/user/Statistic/Atlas_spec_match_review.xlsx"

def oil_of(v):
    """'безмасляный'/'да' -> безмасл; 'масляный'/'нет' -> масл; пусто -> None."""
    s=str(v).strip().lower()
    if not s: return None
    if "безмасл" in s or s in ("да","yes"): return "безмасл"
    if "масл" in s or s in ("нет","no"):    return "масл"
    return None

def load_ours():
    """Наши Atlas-компрессоры: слияние дублей по IE_CODE, спеки + текст-флаги."""
    rows={}
    for r in csv.DictReader(open(SPECS_CSV, encoding="utf-8-sig", errors="replace"), delimiter=";"):
        code=(r.get("IE_CODE") or "").strip()
        if not code: continue
        cur=rows.setdefault(code, {})
        for k,v in r.items():
            if v and not cur.get(k): cur[k]=v.strip()
    # цены/URL из экспорта (слаг ссылки = IE_CODE)
    price={}
    for row in csv.reader(open(PROKO_CSV, encoding="utf-8-sig", errors="replace"), delimiter=";"):
        if len(row)<3 or "prokompressor" not in row[1]: continue
        sl=row[1].rstrip("/").split("/")[-1].lower()
        try: p=float(str(row[2]).replace(",",".").replace(" ","")) or None
        except: p=None
        price[sl]=(row[0].strip().replace("&quot;",'"'), row[1].strip(), p)
    ours=[]
    for code,r in rows.items():
        if "atlas" not in (r.get("IP_PROP22553") or "").lower(): continue
        name=r.get("IE_NAME","")
        if not is_compressor(name+" "+code): continue
        sn=series_num(name+" "+code)
        if not sn: continue
        ff,vsd,rv = text_flags(name+" "+code)
        if rv is None:
            if num(r.get("IP_PROP22564")): rv=num(r.get("IP_PROP22564"))        # объём ресивера, л
            elif str(r.get("IP_PROP22574","")).strip().lower() in ("да","есть"): rv=1
        fl = flow_value(r.get("IP_PROP22571"), "л/мин") or flow_value(r.get("IP_PROP22658"), "м3/мин")
        nm,url,p = price.get(code.lower(), (name, f"https://prokompressor.ru/catalog/{code}/", None))
        ours.append(dict(sn=sn, kw=sane_kw(num(r.get("IP_PROP22562"))),
                         bar=sane_bar(num(r.get("IP_PROP22573"))) or bar_from_text(name+" "+code),
                         fl=fl, oil=oil_of(r.get("IP_PROP22583")), ff=ff, vsd=vsd, rv=rv,
                         name=nm or name, url=url, price=p))
    return ours

def load_comp():
    """Atlas-компрессоры конкурентов: specs объединены по URL (поздний прогон главнее)."""
    names={}; specs={}; price={}; status={}
    for f in SCRAPE_FILES:
        try: fh=open(f, encoding="utf-8-sig", errors="replace")
        except FileNotFoundError: continue
        for r in csv.DictReader(fh):
            u=(r.get("product_url") or "").strip()
            if not u: continue
            names[u]=best_name(names.get(u,""), (r.get("name") or "").strip())
            sp=r.get("specs") or ""
            if sp:
                try: d=json.loads(sp)
                except Exception: d=None
                if isinstance(d, dict) and d: specs.setdefault(u,{}).update(d)
            for col in ("price","old_price"):
                try:
                    v=float(str(r.get(col,"")).replace(",",".").replace(" ",""))
                    if v>0: price[u]=v; break
                except: pass
            if "снят" in (r.get("series_status") or "").lower(): status[u]="снято"
    cands=[]
    for u,nm in names.items():
        if dm(u) not in COMPETITORS: continue
        if not is_product_url(u): continue
        if brand_of(u, nm)!="atlas": continue
        text=(nm or "")+" "+slug(u)
        if not any(ch.isdigit() for ch in text): continue
        if not is_compressor(text): continue
        sn=series_num(text)
        if not sn: continue
        d=specs.get(u, {})
        kw=bar=fl=None; oil=None
        for k,v in d.items():
            kl=k.lower()
            if kw is None and "мощ" in kl and "шум" not in kl and "звук" not in kl: kw=sane_kw(num(v))
            if bar is None and "давлен" in kl: bar=sane_bar(num(v))
            if fl is None and "произв" in kl: fl=flow_value(v, kl+" "+str(v))
            if oil is None and "безмасл" in kl: oil=oil_of(v)
        if kw is None and fl is None: continue   # совсем без спеков — липнет ко всей серии
        # флаги из НАЗВАНИЯ: слаги врут (aero клонирует FF-слаг под P-карточки);
        # слаг — только если названия нет (sitemap-only)
        ff,vsd,rv = text_flags(nm) if nm else text_flags(slug(u))
        cands.append(dict(sn=sn, kw=kw, bar=bar or bar_from_text(text), fl=fl, oil=oil,
                          ff=ff, vsd=vsd, rv=rv, name=nm or slug(u), url=u, site=dm(u),
                          price=price.get(u), status=status.get(u,"")))
    return cands

def why(o, c):
    f=lambda v: ("%g"%v) if v is not None else "—"
    parts=[f"{o['sn'][0].upper()}{'%g'%o['sn'][1]}",
           f"кВт {f(o['kw'])}≈{f(c['kw'])}", f"бар {f(o['bar'])}≈{f(c['bar'])}",
           f"произв {f(o['fl'])}≈{f(c['fl'])}"]
    if o.get("ff"): parts.append("FF")
    if o.get("vsd"): parts.append("VSD")
    if o.get("rv") is not None: parts.append(f"ресивер {f(o['rv'])}≈{f(c.get('rv'))}")
    return " · ".join(parts)

def _ckey(c):
    """Ключ «это один и тот же товар» (клоны карточки на сайте по категориям)."""
    return (c["sn"], c["kw"], c["bar"], c["fl"], c["ff"], c["vsd"], c["rv"],
            re.sub(r"\s+"," ",c["name"].strip().lower()))

def build():
    ours=load_ours(); cands=load_comp()
    by_sn=defaultdict(list)
    for c in cands: by_sn[c["sn"]].append(c)
    print(f"наших Atlas-компрессоров с серией: {len(ours)} | кандидатов у конкурентов: {len(cands)}")
    # матч -> группировка ПО САЙТАМ: 6 сайтов с 1 карточкой = идеал (не «>3 кандидатов»!);
    # неоднозначность = >1 РАЗНОЙ карточки на одном сайте
    clean=[]; ambig=[]; n0=0
    for o in ours:
        m=receiver_filter(o.get("rv"), match(o, by_sn.get(o["sn"], [])))
        per_site=defaultdict(dict)                  # site -> {ckey: best-card}
        for c in m:
            k=_ckey(c); cur=per_site[c["site"]].get(k)
            if cur is None or (c["price"] and (not cur["price"] or c["price"]<cur["price"])):
                per_site[c["site"]][k]=c
        if not per_site: n0+=1; continue
        (ambig if any(len(v)>1 for v in per_site.values()) else clean).append((o, per_site))
    print(f"однозначно (на каждом сайте 1 карточка): {len(clean)} | "
          f"неоднозначные (где-то 2+ разных): {len(ambig)} | без матча: {n0}")

    wb=openpyxl.Workbook(); wb.remove(wb.active)
    blue=Font(color="0563C1", underline="single")
    strike=Font(color="C00000", underline="single", strike=True)
    bold=Font(bold=True, color="FFFFFF"); hfill=PatternFill("solid", fgColor="305496")
    warn=PatternFill("solid", fgColor="FFE699")     # жёлтый: на сайте >1 разной карточки
    nomatch=PatternFill("solid", fgColor="F2F2F2")
    checkfill=PatternFill("solid", fgColor="C6E0B4") # зелёный: карточка есть, цены нет
    center=Alignment(horizontal="center", vertical="center", wrap_text=True)
    for title, rows in (("спек-матч", clean), ("неоднозначные", ambig)):
        ws=wb.create_sheet(title)
        HDR=["№","Наш товар","Ваша цена","Ваша ссылка"]+COMPETITORS\
            +["min конк.","Δ к min, %","Почему сцепилось","ВЕРДИКТ (ок / ошибка: ...)"]
        ws.append(HDR)
        for ci in range(1,len(HDR)+1):
            cell=ws.cell(1,ci); cell.font=bold; cell.fill=hfill; cell.alignment=center
        rows=sorted(rows, key=lambda t:(-len(t[1]), t[0]["name"]))   # больше сайтов — выше
        r=1
        for o,per_site in rows:
            r+=1
            ws.cell(r,1,r-1); ws.cell(r,2,o["name"])
            c3=ws.cell(r,3, o["price"] if o["price"] else "нет цены")
            if o["price"]: c3.number_format="# ##0"
            l=ws.cell(r,4,"открыть"); l.hyperlink=o["url"]; l.font=blue
            comp_prices=[]; first=None
            for ci,site in enumerate(COMPETITORS):
                cell=ws.cell(r,5+ci)
                cards=list(per_site.get(site,{}).values())
                if not cards: cell.fill=nomatch; continue
                priced=[c for c in cards if c["price"] and c["status"]!="снято"]
                show=min(priced, key=lambda c:c["price"]) if priced else cards[0]
                first=first or show
                if show["price"]:
                    cell.value=show["price"]; cell.number_format="# ##0"
                    cell.hyperlink=show["url"]
                    if show["status"]=="снято": cell.font=strike
                    else:
                        cell.font=blue; comp_prices.append(show["price"])
                else:
                    cell.value="снято" if show["status"]=="снято" else "проверить"
                    cell.hyperlink=show["url"]
                    cell.font=strike if show["status"]=="снято" else blue
                    if show["status"]!="снято": cell.fill=checkfill
                if len(cards)>1: cell.fill=warn
            if comp_prices:
                mn=min(comp_prices)
                ws.cell(r,11,mn).number_format="# ##0"
                if o["price"]: ws.cell(r,12, round((o["price"]-mn)/mn*100,1))
            ws.cell(r,13, why(o, first))
        widths=[5,46,10,9]+[13]*len(COMPETITORS)+[11,10,46,24]
        for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
        ws.freeze_panes="C2"; ws.auto_filter.ref=f"A1:{get_column_letter(len(HDR))}{r}"
    wb.save(OUT); print(f"-> {OUT}")

if __name__=="__main__":
    build()
