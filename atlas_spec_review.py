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
from spec_match import (num, sane_kw, sane_bar, bar_value, bar_from_text, flow_value, bar_flow_pairs,
                        series_num, text_flags, is_compressor, match, receiver_filter, ff_filter, card_issue)
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
                         bar=bar_value(r.get("IP_PROP22573")) or bar_from_text(name+" "+code),
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
            # ТОЛЬКО price: old_price = перечёркнутое «было», часто устаревший мусор
            # (pnevmo-sklad «по запросу»: price пуст, old_price=395960 повторяется по серии).
            try:
                v=float(str(r.get("price","")).replace(",",".").replace(" ",""))
                if 100<=v<=50_000_000: price[u]=v   # санити: артикулы в поле цены (99 млрд) и копейки — мимо
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
        kw=None; oil=None; raw_bar=raw_flow=fkey=None
        for k,v in d.items():
            kl=k.lower()
            if kw is None and "мощ" in kl and "шум" not in kl and "звук" not in kl: kw=sane_kw(num(v))
            if raw_bar is None and "давлен" in kl: raw_bar=v
            if raw_flow is None and "произв" in kl: raw_flow=v; fkey=kl
            if oil is None and "безмасл" in kl: oil=oil_of(v)
        # флаги из НАЗВАНИЯ: слаги врут (aero клонирует FF-слаг под P-карточки);
        # слаг — только если названия нет (sitemap-only)
        ff,vsd,rv = text_flags(nm) if nm else text_flags(slug(u))
        srv=None
        for k,v in d.items():
            if "ресивер" in k.lower():
                n=num(v)
                if n and n>=10: srv=n; break
                if str(v).strip().lower() in ("да","есть","yes") and srv is None: srv=1
        if rv is None or (rv==1 and srv and srv>1): rv = srv if srv is not None else rv
        # сдвоенные карточки «8/10» -> кандидат на вариант; цена на странице = за МЛАДШИЙ вариант
        pairs=sorted(bar_flow_pairs(raw_bar, raw_flow, (fkey or "")+" "+str(raw_flow or "")),
                     key=lambda bf:(bf[0] is None, bf[0] or 0))
        for i,(bar,fl) in enumerate(pairs):
            if kw is None and fl is None: continue
            cp=price.get(u) if (len(pairs)==1 or i==0) else None
            cands.append(dict(sn=sn, kw=kw, bar=bar or bar_from_text(text), fl=fl, oil=oil,
                              ff=ff, vsd=vsd, rv=rv, name=nm or slug(u), url=u, site=dm(u),
                              price=cp, status=status.get(u,"")))
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
        m=receiver_filter(o.get("rv"), ff_filter(o.get("ff"), match(o, by_sn.get(o["sn"], []))))
        # группируем кандидатов по ФИЗИЧЕСКОМУ ключу (исполнение/охлаждение/фаза НЕ различаем —
        # их у нас в каталоге нет; AC/WC/Pack одной спеки = один сопоставимый товар, берём дешевле).
        # считаем кол-во исполнений на сайте: ячейку с >1 пометим жёлтым (цена — минимальная).
        per_site=defaultdict(dict); nexec=defaultdict(lambda: defaultdict(int))
        for c in m:
            k=(c["sn"],c["kw"],c["bar"],c["fl"],c["ff"] or 0,c["vsd"] or 0,c["rv"])  # без имени
            nexec[c["site"]][k]+=1; cur=per_site[c["site"]].get(k)
            if cur is None or (c["price"] and (not cur["price"] or c["price"]<cur["price"])):
                per_site[c["site"]][k]=c
        if not per_site: n0+=1; continue
        # неоднозначно = на сайте >1 РАЗНОЙ физ-модели (разные спеки), а не просто исполнения
        (ambig if any(len(v)>1 for v in per_site.values()) else clean).append((o, per_site, nexec))
    matched={id(o) for o,_,_ in clean+ambig}   # сматчилось -> спека уже сошлась с конкурентом
    print(f"однозначно (на каждом сайте 1 карточка): {len(clean)} | "
          f"неоднозначные (где-то 2+ разных): {len(ambig)} | без матча: {n0}")

    wb=openpyxl.Workbook(); wb.remove(wb.active)
    blue=Font(color="0563C1", underline="single")
    strike=Font(color="C00000", underline="single", strike=True)
    bold=Font(bold=True, color="FFFFFF"); hfill=PatternFill("solid", fgColor="305496")
    warn=PatternFill("solid", fgColor="FFE699")     # жёлтый: на сайте >1 разной карточки
    nomatch=PatternFill("solid", fgColor="F2F2F2")
    center=Alignment(horizontal="center", vertical="center", wrap_text=True)
    chkfill=PatternFill("solid", fgColor="FCE4D6")   # сматчился, но спека под вопросом
    for title, rows in (("спек-матч", clean), ("неоднозначные", ambig)):
        ws=wb.create_sheet(title)
        HDR=["№","Наш товар","Ваша цена"]+COMPETITORS\
            +["min конк.","Δ к min, %","Почему сцепилось","Проверить карточку","ВЕРДИКТ (ок / ошибка: ...)"]
        ws.append(HDR)
        for ci in range(1,len(HDR)+1):
            cell=ws.cell(1,ci); cell.font=bold; cell.fill=hfill; cell.alignment=center
        rows=sorted(rows, key=lambda t:(-len(t[1]), t[0]["name"]))   # больше сайтов — выше
        r=1
        for o,per_site,nexec in rows:
            r+=1
            ws.cell(r,1,r-1); ws.cell(r,2,o["name"])
            c3=ws.cell(r,3, o["price"] if o["price"] else "нет цены")
            if o["price"]: c3.number_format="# ##0"
            c3.hyperlink=o["url"]; c3.font=blue          # цена = ссылка на нашу карточку
            comp_prices=[]; first=None
            for ci,site in enumerate(COMPETITORS):
                cell=ws.cell(r,4+ci)
                cards=list(per_site.get(site,{}).values())
                if not cards: cell.fill=nomatch; continue
                priced=[c for c in cards if c["price"] and c["status"]!="снято"]
                show=min(priced, key=lambda c:c["price"]) if priced else cards[0]
                first=first or show
                multi_exec=any(n>1 for n in nexec.get(site,{}).values())   # слиты исполнения
                if show["price"]:
                    cell.value=show["price"]; cell.number_format="# ##0"
                    cell.hyperlink=show["url"]
                    if show["status"]=="снято": cell.font=strike
                    else:
                        cell.font=blue; comp_prices.append(show["price"])
                else:
                    cell.value="снято" if show["status"]=="снято" else "По запросу"
                    cell.hyperlink=show["url"]
                    cell.font=strike if show["status"]=="снято" else blue
                # жёлтый: >1 разной физ-модели ИЛИ слиты исполнения (цена — минимальная)
                if len(cards)>1 or multi_exec: cell.fill=warn
            if comp_prices:
                mn=min(comp_prices)
                ws.cell(r,10,mn).number_format="# ##0"
                if o["price"]: ws.cell(r,11, round((o["price"]-mn)/mn*100,1))
            ws.cell(r,12, why(o, first))
            iss=card_issue(o, by_sn.get(o["sn"], []))   # сматчился, но спека не подтверждена
            if iss:
                label,ov,v1,nd,ratio,src=iss
                cc=ws.cell(r,13, f"{label}: у нас {ov:g} vs {v1:g} ({nd} сайт.)")
                cc.hyperlink=src["url"]; cc.font=Font(color="C55A11", underline="single")
                ws.cell(r,2).fill=chkfill
        widths=[5,46,12]+[13]*len(COMPETITORS)+[11,10,46,30,24]
        for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
        ws.freeze_panes="C2"; ws.auto_filter.ref=f"A1:{get_column_letter(len(HDR))}{r}"

    # ЛИСТ 3: GAP — модели Atlas, которых у нас НЕТ (серия+номер не в каталоге), ≥2 сайта
    our_sn={o["sn"] for o in ours}
    groups=defaultdict(lambda: defaultdict(list))    # gkey -> site -> [cards]
    for c in cands:
        if c["sn"] in our_sn: continue                # серия у нас есть -> не gap (см. «Проверить карточку»)
        gk=(c["sn"], round(c["kw"]) if c["kw"] else None,
            round(c["bar"]) if c["bar"] else None, c["ff"] or 0, c["vsd"] or 0)
        groups[gk][c["site"]].append(c)
    gap=[(gk,sites) for gk,sites in groups.items() if len(sites)>=2]
    gap.sort(key=lambda t:-len(t[1]))
    ws=wb.create_sheet("GAP — нет у нас")
    HDR=["№","Модель (у конкурентов, нас нет)","Серия","кВт","бар","Сайтов"]+COMPETITORS+["min конк."]
    ws.append(HDR)
    for ci in range(1,len(HDR)+1):
        cell=ws.cell(1,ci); cell.font=bold; cell.fill=hfill; cell.alignment=center
    r=1
    for gk,sites in gap:
        r+=1; allc=[c for cs in sites.values() for c in cs]
        nm=max(allc, key=lambda c:len(c["name"]))["name"]
        ws.cell(r,1,r-1); ws.cell(r,2,nm)
        ws.cell(r,3, f"{gk[0][0].upper()}{gk[0][1]:g}"); ws.cell(r,4,gk[1] or ""); ws.cell(r,5,gk[2] or "")
        ws.cell(r,6,len(sites))
        prices=[]
        for ci,site in enumerate(COMPETITORS):
            cell=ws.cell(r,7+ci); cs=sites.get(site)
            if not cs: cell.fill=nomatch; continue
            priced=[c for c in cs if c["price"] and c["status"]!="снято"]
            show=min(priced,key=lambda c:c["price"]) if priced else cs[0]
            if show["price"]:
                cell.value=show["price"]; cell.number_format="# ##0"; cell.hyperlink=show["url"]
                cell.font=strike if show["status"]=="снято" else blue
                if show["status"]!="снято": prices.append(show["price"])
            else:
                cell.value="снято" if show["status"]=="снято" else "По запросу"
                cell.hyperlink=show["url"]; cell.font=strike if show["status"]=="снято" else blue
            if len(cs)>1: cell.fill=warn
        if prices: ws.cell(r,13,min(prices)).number_format="# ##0"
    widths=[5,52,9,7,7,8]+[13]*len(COMPETITORS)+[11]
    for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
    ws.freeze_panes="B2"; ws.auto_filter.ref=f"A1:{get_column_letter(len(HDR))}{r}"
    print(f"GAP-моделей (нет у нас, ≥2 сайта): {len(gap)}")

    # ЛИСТ 4: Проверить карточку — наша спека против конкурентов ТОЙ ЖЕ модели (по полю:
    # card_issue само-валидирует — не прячет ×10-ошибку при матче к карточке без поля).
    orange=Font(color="C55A11", underline="single")
    ws=wb.create_sheet("Проверить карточку")
    HDR=["№","Наш товар","Ваша цена","Что не так (спека)","Подтверждение (конкурент)"]
    ws.append(HDR)
    for ci in range(1,len(HDR)+1):
        cell=ws.cell(1,ci); cell.font=bold; cell.fill=hfill; cell.alignment=center
    r=1
    for o in sorted(ours, key=lambda o:o["name"]):
        iss=card_issue(o, by_sn.get(o["sn"], []))
        if not iss: continue
        label,ov,v1,nd,ratio,src=iss
        r+=1
        ws.cell(r,1,r-1); ws.cell(r,2,o["name"])
        c3=ws.cell(r,3, o["price"] if o["price"] else "нет цены")
        if o["price"]: c3.number_format="# ##0"
        c3.hyperlink=o["url"]; c3.font=blue
        ws.cell(r,4, f"{label}: у нас {ov:g}, у конкур. {v1:g} ({nd} сайт.)").font=orange
        lk=ws.cell(r,5, f"[{src['site']}] {src['name'][:50]}"); lk.hyperlink=src["url"]; lk.font=blue
    widths=[5,46,12,40,52]
    for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
    ws.freeze_panes="B2"; ws.auto_filter.ref=f"A1:{get_column_letter(len(HDR))}{r}"
    print(f"Проверить карточку (наша спека спорит с ≥2 сайтами): {r-1}")
    wb.save(OUT); print(f"-> {OUT}")

if __name__=="__main__":
    build()
