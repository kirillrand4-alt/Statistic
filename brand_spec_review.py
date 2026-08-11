"""Раскатка спек-матча на ВСЕ бренды. Per-brand xlsx, 5 листов:
  спек-матч / неоднозначные / GAP (нет у нас) / Проверить карточку / Снятые у конкурентов.
Atlas — спец-regex серий (SER_ATLAS); остальные — дженерик «слово+число» (дефис/точка
внутри слова склеиваются: K-MAX=KMAX; стоп-слова и бренд-токены пропускаются).
Исполнения одной физ-спеки (AC/WC/Pack/фаза) = один товар (жёлтая ячейка, цена min)."""
import csv, sys, json, re, os, zipfile
csv.field_size_limit(sys.maxsize)
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from collections import defaultdict, Counter
from matcher import brand_of, BRAND_ALIASES, brand_from_text
from spec_match import (num, sane_kw, bar_value, bar_from_text, flow_value, bar_flow_pairs,
                        series_num, text_flags, is_compressor, match, receiver_filter, ff_filter,
                        ip_filter, ip_class, cool_filter, cool_class, is_flow_key, card_issue,
                        prefer_exact_variant, VARIANT_MARKS, model_code,
                        variant_letters, same_variant)
from atlas_need_specs import is_product_url, slug, dm, best_name, load_universe
from scrape_files import U, OURS_DIR, find_ours

# Наш каталог: выгрузка свойств из Битрикса + выгрузка цен. Имена файлов
# меняются от выгрузки к выгрузке (products_export_20260608, _20260711, …),
# поэтому берём последний подходящий из OURS_DIR, а не прибиваем имя гвоздями.
SPECS_CSV = find_ours("specs_compact", "specs2/specs_compact.csv")
PROKO_CSV = find_ours("products_export", "e7171060-products_export_20260608.csv")
OUTDIR = "/home/user/Statistic/brand_reports"
ZIP    = "/home/user/Statistic/Brands_spec_match.zip"
COMPETITORS = ["compressortyt.ru","aerocompressors.ru","pnevmoteh.ru",
               "pnevmo-sklad.ru","v-p-k.ru","rutector.ru"]

# --- дженерик-серия: первое «слово+число» после чистки -----------------------------------
_STOPW = {"компрессор","компрессора","компрессоры","kompressor","винтовой","vintovoy","vintovoj",
    "поршневой","porshnevoy","porshnevoj","спиральный","безмасляный","bezmaslyanyy","дизельный",
    "dizelnyy","электрический","elektricheskiy","передвижной","peredvizhnoy","масляный",
    "ременный","remennyy","прямой","privod","привод","серия","серии","model","модель","тип",
    "ip","квт","kvt","kw","бар","bar","атм","atm","гц","hz","фаз","ph","шт","мм","кг","до","от",
    "для","с","на","и","в","quot","plus","vsd","ff","pack","silenced","unsilenced","trolley",
    "block","receiver","ресивер","resiver","tm","fm","ас","wc","ac"}   # fm=floor mounted (антоним tm)
# Кириллица → латиница для product-кодов: наш каталог пишет ЕКО/КМ/ВК/АА (кириллица),
# конкуренты — EKO/KM/VK/AA (латиница). Только «чистые» омофоны: в→v, н→n и визуальные
# а/е/о/к/м/р/т/х. Исключены с→c/s (СБ=SB, неоднозначно) и буквы без аналога.
_CYR2LAT = str.maketrans('аеокмртхвн', 'aeokmrtxvn')

def _with_variant(sn, text):
    """Метку исполнения (DF/ID/DRY) приклеить к имени семейства, ГДЕ БЫ она ни стояла.

    Схема хвостовых суффиксов ловит метку, только если она идёт сразу за номером
    («NT7 DF 500» -> ntdf), и не ловит, когда между ними влезло давление
    («NT7 13DF 500» -> nt). Из-за этой асимметрии наша карточка и карточка того же
    конкурента с тем же исполнением попадали в РАЗНЫЕ семейства и не могли
    встретиться, а встречалась «голая»:
        наш ARIACOM NT7 13DF 500  ->  aerocompressors NT7 500      (ложь, 420 vs 370 кг)
        рядом лежала NT7 DF 500   ->  семейство ntdf, до сравнения не доходила
    Замер 11.08 на всех брендах (вместе с послаблением ff ниже): снимает 143 таких
    пары (dalgakiran 124, ariacom 17, cross 2), добавляет 4 верных, из 42
    подтверждённых верных сцепок не теряет ни одной, новых пар с ЧУЖИМ исполнением
    не создаёт. Atlas не трогаем: там метка FF симметрична с обеих сторон и в
    семейство не попадает ни у кого.
    """
    if not sn: return sn
    fam, n = sn
    t = re.sub(r"(\d)([a-zа-я])", r"\1 \2", str(text).lower().replace("_"," ").replace("-"," "))
    for mk in sorted({m.group(1).lower() for m in VARIANT_MARKS.finditer(t)}):
        if mk not in fam: fam += mk
    return (fam, n)

def gen_series(text, brand):
    s=" "+str(text).lower().replace("_"," ")+" "
    s=re.sub(r"\b(new|новый|новая|нов)\b"," ",s)         # маркетинг-слова не серия (COMARO MD NEW 55)
    s=re.sub(r"(?<=[a-zа-я])[\-.](?=[a-zа-я])","",s)     # k-max -> kmax, dr.sonic -> drsonic
    s=re.sub(r"(?<=[a-zа-я])\.(?=\d)"," ",s)             # genesis i.18,5 -> i 18,5 (десятичные 18.5 целы)
    s=s.replace("-"," ").replace("/"," ")
    btoks={brand}|{a for a,c in BRAND_ALIASES.items() if c==brand}
    UNITS={"l","kw","hp","ph","db","v","w","bar","atm","psi","min","mm","kg"}
    for m in re.finditer(r"\b([a-zа-я]{2,12})(\+?)(?:\s+([a-z]))?\s*(\d+(?:[.,]\d+)?)"
                         r"\s*([a-z]{1,4}(?![a-zа-я]))?(?:\s+([a-z]{1,4}(?![a-zа-я])))?(?!\d)", s):
        w=m.group(1)
        # «plus» сразу после бренда перед числом = РЕАЛЬНАЯ серия (Fini PLUS 11-08), не маркетинг.
        # В «VEGA 11 R PLUS» серией становится vega (раньше в строке) — plus туда не попадёт.
        if (w in _STOPW and w!="plus") or w in btoks: continue
        # одиночная ЛАТИНСКАЯ буква между серией и числом = вариант линейки (GENESIS I = инвертор);
        # кириллические одиночки (предлоги «с»/«и») игнорируются.
        # «+» приклеенный к числу (ARIACOM HCA+110) = часть имени серии (HCA+ ≠ HCA), пишется
        # одинаково с обеих сторон — раньше валил извлечение (попадал в «серия не извлекается»).
        # До ДВУХ латинских токенов ПОСЛЕ числа = вариант модели: Ozen OSC 110D/110U/110S,
        # Ekomak DMD 100 C / CR / CRD / C STD — разные заводские sku (доказано V1-артикулами).
        # Кириллицу (пВ у KraftMachine) не берём. «plus»/маркетинг — стоп-слова везде.
        suf="".join(t for t in (m.group(5), m.group(6)) if t and t not in UNITS and t not in _STOPW)
        return ((w+m.group(2)+(m.group(3) or "")+suf).translate(_CYR2LAT), float(m.group(4).replace(",",".")))
    # FALLBACK: одно-буквенная серия (Atom А-11, Boge C 10, Comprag D-11, Dalgakiran F 11,
    # IR R110). Срабатывает ТОЛЬКО если основной проход вернул None — старые извлечения не
    # затрагиваются. Латиница всегда; кириллица — кроме предлогов/союзов.
    _CYR_PREP={"с","и","в","у","о","к","я","на","до","от","по","за","не","со","из"}  # «а» = серия Atom
    for m in re.finditer(r"\b([a-zа-я])\s*[- ]?\s*(\d+(?:[.,]\d+)?)"
                         r"\s*([a-z]{1,4}(?![a-zа-я]))?(?!\d)", s):
        w=m.group(1)
        if w in btoks or (re.match(r"[а-я]", w) and w in _CYR_PREP): continue
        suf=m.group(3) if (m.group(3) and m.group(3) not in UNITS and m.group(3) not in _STOPW) else ""
        return ((w+suf).translate(_CYR2LAT), float(m.group(2).replace(",",".")))
    return None

# Экспериментальный режим (по умолчанию ВЫКЛЮЧЕН): буквы уходят из ключа семейства в
# метку исполнения, которая сравнивается отдельно (см. variant_filter). Замер 11.08 на
# боевых данных: снимает 7 ложных сцепок из 8 доказанных, возвращает 25 верных пар из 32
# подтверждённых агентами, притаскивает 2 ложных из 27 опровергнутых, но теряет 6 из 42
# ранее подтверждённых и уменьшает общее число матчей на 658. Пока эти 658 не проверены
# выборочно — режим не включаем. Включение: VARIANT_STRICT=1.
VARIANT_STRICT = os.getenv("VARIANT_STRICT", "").strip() in ("1", "true", "yes")


def base_family(text, brand):
    """Семейство без буквенного хвоста: первый буквенный токен кода модели + номер.
       наш «ET SL 45 H AC 10 бар»               -> ('sl', 45)
       их  «ET-Compressors SL 45 HAC (IP23) 10» -> ('sl', 45)"""
    sn = gen_series(text, brand)
    if not sn: return sn
    for t in model_code(text, brand)[0]:
        if not re.fullmatch(r"[\d.]+", t):
            return (t.translate(_CYR2LAT), sn[1])
    return sn


def ser_of(text, brand):
    if brand=="atlas": return series_num(text)
    if VARIANT_STRICT: return base_family(text, brand)
    return _with_variant(gen_series(text, brand), text)


def variant_filter(o_name, cands, brand):
    """Направленное правило исполнения — в форме receiver_filter/ff_filter.

    Если среди кандидатов есть карточка с ТОЙ ЖЕ меткой — оставляем только такие.
    Если нет, а метки у кандидатов вообще проставлены — значит конкурент исполнения
    различает, а нашего не возит: режем. Если ни у кого меток нет — молчание,
    совместимо. Работает только при VARIANT_STRICT."""
    ours = variant_letters(o_name, brand)
    marks = [(c, variant_letters(c.get("name") or slug(c["url"]).replace("_"," "), brand))
             for c in cands]
    exact = [c for c, m in marks if same_variant(m, ours)]
    if exact: return exact
    if ours and any(m for _, m in marks): return []
    return cands

_DTAIL=re.compile(r'[\d\)лl]\s*[-–]?\s*([дd])\s*(?:\(.*)?$', re.I)   # «270L D», «500Д», «10Д (с осуш.)»
_VSTAIL=re.compile(r'(?:\d|\))\s*(вс|bc)\s*$', re.I)                  # «ВК100Р-10ВС»
_OTAIL=re.compile(r'/[oо][w2]?\s*$', re.I)                            # Zammer «…-500/O», /OW, /O2 = осушитель
def suffix_flags(name, brand, ff, rv):
    """Хвостовые маркеры НЕ-Atlas брендов (у Atlas 'Dd'=дизель, не трогаем):
    Д/D после числа/л = осушитель; ВС = воздухосборник (ресивер упомянут)."""
    if brand=="atlas": return ff, rv
    nm=str(name).strip()
    if ff is None and (_DTAIL.search(nm) or _OTAIL.search(nm) or "с осушителем" in nm.lower()): ff=1
    if rv is None and _VSTAIL.search(nm): rv=1
    return ff, rv

# --- Berg: суффикс-схема заводских кодов ВК (ПОДТВЕРЖДЕНА пропами нашего каталога):
# Р=ременный привод, Е=частотник(VSD), О=осушитель; комбинации РЕ/РО/РЕО. Буквы клеятся
# к числу (ВК-18.5РО-500) или идут одиночными токенами сразу после него (ВК-11 Е 10).
_BERG_SUF=re.compile(r'(?:вк|vk)[- ]?\d+(?:[.,]\d+)?([а-яa-z]{0,3})', re.I)
def berg_suffix(text):
    """set ⊆ {r,e,o} для ВК-имён Berg; None, если имя не по ВК-схеме."""
    t=str(text).lower().replace("_"," ")
    m=_BERG_SUF.search(t)
    if not m: return None
    suf=set(m.group(1).translate(_CYR2LAT))
    for tok in re.split(r'[-\s/(),]+', t[m.end():]):   # одиночные буквы до первой цифры
        if len(tok)==1 and tok.isalpha(): suf.add(tok.translate(_CYR2LAT))
        elif tok: break
    return suf & {"r","e","o"}

def oil_of(v):
    s=str(v).strip().lower()
    if not s: return None
    if "безмасл" in s or s in ("да","yes"): return "безмасл"
    if "масл" in s or s in ("нет","no"):    return "масл"
    return None

# --- наши товары по брендам ---------------------------------------------------------------
def load_ours_all():
    rows={}
    for r in csv.DictReader(open(SPECS_CSV, encoding="utf-8-sig", errors="replace"), delimiter=";"):
        code=(r.get("IE_CODE") or "").strip()
        if not code: continue
        cur=rows.setdefault(code, {})
        for k,v in r.items():
            if v and not cur.get(k): cur[k]=v.strip()
    price={}
    for row in csv.reader(open(PROKO_CSV, encoding="utf-8-sig", errors="replace"), delimiter=";"):
        if len(row)<3 or "prokompressor" not in row[1]: continue
        sl=row[1].rstrip("/").split("/")[-1].lower()
        try: p=float(str(row[2]).replace(",",".").replace(" ","")) or None
        except: p=None
        price[sl]=(row[0].strip().replace("&quot;",'"'), row[1].strip(), p)
    ours=defaultdict(list)
    for code,r in rows.items():
        man=(r.get("IP_PROP22553") or "").strip()
        b=brand_from_text(man) or BRAND_ALIASES.get(man.lower().split()[0] if man else "", None)
        if not b: continue
        name=r.get("IE_NAME","")
        if not is_compressor(name+" "+code): continue
        sn=ser_of(name+" "+code, b)
        if not sn: continue
        ff,vsd,rv = text_flags(name+" "+code)
        ff,rv = suffix_flags(name, b, ff, rv)
        if rv is None:
            if num(r.get("IP_PROP22564")): rv=num(r.get("IP_PROP22564"))
            elif str(r.get("IP_PROP22574","")).strip().lower() in ("да","есть"): rv=1
        fl = flow_value(r.get("IP_PROP22571"), "л/мин") or flow_value(r.get("IP_PROP22658"), "м3/мин")
        nm,url,p = price.get(code.lower(), (name, f"https://prokompressor.ru/catalog/{code}/", None))
        wev=num(r.get("IP_PROP22555")); drv=(r.get("IP_PROP22601") or "").strip().lower() or None
        if drv: drv="ремен" if "ремен" in drv else ("прямой" if "прям" in drv else None)
        if str(r.get("IP_PROP22565","")).strip().lower()=="да": ff=1   # проп «осушитель» (направл. флаг — безопасно)
        pv=str(r.get("IP_PROP22586","")).strip().lower()   # проп «частотник»: знание да/нет
        if vsd is None and pv:                             # имя-маркер приоритетнее пропа
            vsd = 1 if pv=="да" else (0 if pv=="нет" else None)
        ours[b].append(dict(sn=sn, kw=sane_kw(num(r.get("IP_PROP22562"))),
                            bar=bar_value(r.get("IP_PROP22573")) or bar_from_text(name+" "+code),
                            fl=fl, oil=oil_of(r.get("IP_PROP22583")), ff=ff, vsd=vsd, rv=rv,
                            name=nm or name, url=url, price=p, ip=ip_class(name+" "+code),
                            cool=cool_class(name+" "+code, r.get("IP_PROP22669")),
                            we=(wev if wev and 1<=wev<=50000 else None), dr=drv))
    return ours

# --- конкуренты по брендам ----------------------------------------------------------------
def load_comp_all():
    names, specs, _ = load_universe()
    price={}; status={}; skus={}
    import scrape_files
    for f in scrape_files.SCRAPE_FILES:
        try: fh=open(f, encoding="utf-8-sig", errors="replace")
        except FileNotFoundError: continue
        for r in csv.DictReader(fh):
            u=(r.get("product_url") or "").strip()
            if not u: continue
            sk=(r.get("sku") or "").strip()
            if sk: skus[u]=sk
            try:
                v=float(str(r.get("price","")).replace(",",".").replace(" ",""))
                if 100<=v<=50_000_000: price[u]=v   # санити: артикулы в поле цены (99 млрд) и копейки — мимо
            except: pass
            st=(r.get("series_status") or "").lower()
            if "снят" in st and "v-p-k.ru/catalog" not in u: status[u]="снято"
    cands=defaultdict(list)
    for u,nm in names.items():
        if dm(u) not in COMPETITORS or not is_product_url(u): continue
        b=brand_of(u, nm)
        if not b: continue
        text=(nm or "")+" "+slug(u)
        if not any(ch.isdigit() for ch in text) or not is_compressor(text): continue
        sn=ser_of(text, b)
        if not sn: continue
        d=specs.get(u, {})
        kw=None; oil=None; raw_bar=raw_flow=fkey=None; cool_raw=""
        for k,v in d.items():
            kl=k.lower()
            if kw is None and "мощ" in kl and "шум" not in kl and "звук" not in kl: kw=sane_kw(num(v))
            if raw_bar is None and "давлен" in kl: raw_bar=v
            if raw_flow is None and is_flow_key(k): raw_flow=v; fkey=kl
            if oil is None and "безмасл" in kl: oil=oil_of(v)
            if not cool_raw and "охлажд" in kl: cool_raw=str(v)
        ff,vsd,rv = text_flags(nm) if nm else text_flags(slug(u))
        ff,rv = suffix_flags(nm or slug(u), b, ff, rv)
        bsuf = berg_suffix(text) if b=="berg" else None
        if bsuf is not None:           # заводская схема ВК: код модели ПОЛНЫЙ, отсутствие
            vsd = 1 if "e" in bsuf else 0          # буквы = знаем-нет (не «не указано»)
            if "o" in bsuf: ff=1
        we=dr=sku2=None
        for k,v in d.items():
            kl=k.lower()
            if we is None and ("вес" in kl or "масса" in kl) and "кг" not in str(v).lower()[:0]:
                n=num(v)
                if n and 1<=n<=50000: we=n
            if dr is None and "привод" in kl:
                vl=str(v).lower()
                dr="ремен" if "ремен" in vl else ("прямой" if "прям" in vl else None)
            if sku2 is None and "артикул" in kl: sku2=str(v).strip()
            if ff is None and "осушит" in kl and str(v).strip().lower() in ("да","есть","yes"):
                ff=1   # спек-ключ «С осушителем: да» (Zammer /O и др.)
            if vsd is None and "частот" in kl:     # «Частотный преобразователь: да/нет»;
                vl=str(v).strip().lower()          # «Частота тока: 50» отсеется значением
                if vl in ("да","есть","yes") or "частотн" in vl: vsd=1
                elif vl in ("нет","no"): vsd=0
        if bsuf is not None:   # ВК-схема Berg: Р в коде = ременный, отсутствие = прямой
            dr = "ремен" if "r" in bsuf else "прямой"
        srv=None
        for k,v in d.items():
            if "ресивер" in k.lower():
                n=num(v)
                if n and n>=10: srv=n; break
                if str(v).strip().lower() in ("да","есть","yes") and srv is None: srv=1
        if rv is None or (rv==1 and srv and srv>1): rv = srv if srv is not None else rv
        # сдвоенные карточки «8/10» -> кандидат на каждый вариант; одна цена на странице =
        # за МЛАДШИЙ (базовый/дешёвый) вариант, на старшие цену не вешаем (она была бы занижена)
        pairs=sorted(bar_flow_pairs(raw_bar, raw_flow, (fkey or "")+" "+str(raw_flow or "")),
                     key=lambda bf:(bf[0] is None, bf[0] or 0))
        for i,(bar,fl) in enumerate(pairs):
            if kw is None and fl is None: continue
            cp=price.get(u) if (len(pairs)==1 or i==0) else None
            cands[b].append(dict(sn=sn, kw=kw, bar=bar or bar_from_text(text), fl=fl, oil=oil,
                                 ff=ff, vsd=vsd, rv=rv, name=nm or slug(u), url=u, site=dm(u),
                                 price=cp, status=status.get(u,""), ip=ip_class(text),
                                 cool=cool_class(text, cool_raw),
                                 we=we, dr=dr, sku=skus.get(u) or sku2))
    return cands

# --- стили --------------------------------------------------------------------------------
def _styles():
    return dict(blue=Font(color="0563C1", underline="single"),
        strike=Font(color="C00000", underline="single", strike=True),
        bold=Font(bold=True, color="FFFFFF"), hfill=PatternFill("solid", fgColor="305496"),
        warn=PatternFill("solid", fgColor="FFE699"), nomatch=PatternFill("solid", fgColor="F2F2F2"),
        orange=Font(color="C55A11", underline="single"),
        center=Alignment(horizontal="center", vertical="center", wrap_text=True))

def _hdr(ws, HDR, st):
    ws.append(HDR)
    for ci in range(1,len(HDR)+1):
        c=ws.cell(1,ci); c.font=st["bold"]; c.fill=st["hfill"]; c.alignment=st["center"]

FIELDS=[("кВт","kw",0.06),("бар","bar",0.10),("произв","fl",0.04)]

def why(o, c):
    f=lambda v: ("%g"%v) if v is not None else "—"
    parts=[f"{str(o['sn'][0]).upper()}{o['sn'][1]:g}", f"кВт {f(o['kw'])}≈{f(c['kw'])}",
           f"бар {f(o['bar'])}≈{f(c['bar'])}", f"произв {f(o['fl'])}≈{f(c['fl'])}"]
    if o.get("ff"): parts.append("FF")
    if o.get("vsd"): parts.append("VSD")
    if o.get("rv") is not None: parts.append(f"ресивер {f(o['rv'])}≈{f(c.get('rv'))}")
    if o.get("cool"): parts.append("вод.охл" if o["cool"]=="water" else "возд.охл")
    # особая отметка (правило заказчика): «не указано» матчится, но подсвечивается.
    # Помечаем только рисковый случай: одна сторона ДА, вторая молчит (+35% к цене);
    # «знаем-нет ↔ молчит» не шумим — это типовой фикс без частотника.
    if o.get("vsd")==1 and c.get("vsd") is None:
        parts.append("⚠ VSD у конкурента не подтверждён")
    elif c.get("vsd")==1 and o.get("vsd") is None:
        parts.append("⚠ у конкурента VSD, у нас не указано")
    return " · ".join(parts)

def build_brand(brand, title, ours, cands, po=None):
    by_sn=defaultdict(list)
    for c in cands: by_sn[c["sn"]].append(c)
    clean=[]; ambig=[]; n0=0
    for o in ours:
        m=receiver_filter(o.get("rv"), ff_filter(o.get("ff"),
            cool_filter(o.get("cool"), ip_filter(o.get("ip"), match(o, by_sn.get(o["sn"], [])))),
            o.get("name","")))
        # Последним шагом — предпочесть кандидата с ТЕМ ЖЕ исполнением, если он есть
        # у конкурента отдельным SKU (проверка 11.08: так было в 6 ложных матчах из 8).
        m=(variant_filter(o.get("name",""), m, brand) if VARIANT_STRICT
           else prefer_exact_variant(o.get("name",""), m))
        per=defaultdict(dict); nexec=defaultdict(lambda: defaultdict(int))
        for c in m:
            k=(c["sn"],c["kw"],c["bar"],c["fl"],c["ff"] or 0,c["vsd"] or 0,c["rv"])
            nexec[c["site"]][k]+=1; cur=per[c["site"]].get(k)
            if cur is None or (c["price"] and (not cur["price"] or c["price"]<cur["price"])):
                per[c["site"]][k]=c
        if not per: n0+=1; continue
        (ambig if any(len(v)>1 for v in per.values()) else clean).append((o, per, nexec))
    matched={id(o) for o,_,_ in clean+ambig}

    st=_styles()
    wb=openpyxl.Workbook(); wb.remove(wb.active)
    # 1-2: матчи
    chkfill=PatternFill("solid", fgColor="FCE4D6")   # сматчился, но спека под вопросом
    for tname, rows in (("спек-матч", clean), ("неоднозначные", ambig)):
        ws=wb.create_sheet(tname)
        HDR=["№","Наш товар","Ваша цена"]+COMPETITORS+["min конк.","Δ к min, %","Почему сцепилось","Проверить карточку","ВЕРДИКТ"]
        _hdr(ws, HDR, st)
        rows=sorted(rows, key=lambda t:(-len(t[1]), t[0]["name"]))
        r=1
        for o,per,nexec in rows:
            r+=1
            ws.cell(r,1,r-1); ws.cell(r,2,o["name"])
            c3=ws.cell(r,3, o["price"] if o["price"] else "нет цены")
            if o["price"]: c3.number_format="# ##0"
            c3.hyperlink=o["url"]; c3.font=st["blue"]
            comp_prices=[]; first=None
            for ci,site in enumerate(COMPETITORS):
                cell=ws.cell(r,4+ci); cards=list(per.get(site,{}).values())
                if not cards: cell.fill=st["nomatch"]; continue
                priced=[c for c in cards if c["price"] and c["status"]!="снято"]
                show=min(priced, key=lambda c:c["price"]) if priced else cards[0]
                first=first or show
                if show["price"]:
                    cell.value=show["price"]; cell.number_format="# ##0"; cell.hyperlink=show["url"]
                    if show["status"]=="снято": cell.font=st["strike"]
                    else: cell.font=st["blue"]; comp_prices.append(show["price"])
                else:
                    cell.value="снято" if show["status"]=="снято" else "По запросу"
                    cell.hyperlink=show["url"]
                    cell.font=st["strike"] if show["status"]=="снято" else st["blue"]
                if len(cards)>1 or any(n>1 for n in nexec.get(site,{}).values()): cell.fill=st["warn"]
            if comp_prices:
                mn=min(comp_prices)
                ws.cell(r,10,mn).number_format="# ##0"
                if o["price"]: ws.cell(r,11, round((o["price"]-mn)/mn*100,1))
            ws.cell(r,12, why(o, first))
            iss=card_issue(o, by_sn.get(o["sn"], []))   # сматчился, но спека не подтверждена
            if iss:
                label,ov,v1,nd,ratio,src=iss
                cc=ws.cell(r,13, f"{label}: у нас {ov:g} vs {v1:g} ({nd} сайт.)")
                cc.hyperlink=src["url"]; cc.font=st["orange"]
                ws.cell(r,2).fill=chkfill
        widths=[5,46,12]+[13]*6+[11,10,46,30,22]
        for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
        ws.freeze_panes="C2"; ws.auto_filter.ref=f"A1:{get_column_letter(len(HDR))}{r}"
    # 3: GAP
    our_sn={o["sn"] for o in ours}
    groups=defaultdict(lambda: defaultdict(list))
    for c in cands:
        if c["sn"] in our_sn: continue
        gk=(c["sn"], round(c["kw"]) if c["kw"] else None, round(c["bar"]) if c["bar"] else None,
            c["ff"] or 0, c["vsd"] or 0, c["rv"], c.get("cool"))   # возд/вод = разные товары
        groups[gk][c["site"]].append(c)
    gap=[(gk,s) for gk,s in groups.items() if len(s)>=2]
    gap.sort(key=lambda t:-len(t[1]))
    ws=wb.create_sheet("GAP — нет у нас")
    HDR=["№","Модель (у конкурентов, нас нет)","Серия","кВт","бар","Сайтов"]+COMPETITORS+["min конк."]
    _hdr(ws, HDR, st); r=1
    for gk,sites in gap:
        r+=1; allc=[c for cs in sites.values() for c in cs]
        ws.cell(r,1,r-1); ws.cell(r,2, max(allc,key=lambda c:len(c["name"]))["name"])
        ws.cell(r,3, f"{str(gk[0][0]).upper()}{gk[0][1]:g}"); ws.cell(r,4,gk[1] or ""); ws.cell(r,5,gk[2] or "")
        ws.cell(r,6,len(sites)); prices=[]
        for ci,site in enumerate(COMPETITORS):
            cell=ws.cell(r,7+ci); cs=sites.get(site)
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
            if len(cs)>1: cell.fill=st["warn"]
        if prices: ws.cell(r,13,min(prices)).number_format="# ##0"
    widths=[5,52,10,7,7,8]+[13]*6+[11]
    for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
    ws.freeze_panes="B2"; ws.auto_filter.ref=f"A1:{get_column_letter(len(HDR))}{r}"
    n_gap=len(gap)
    # 4: Проверить карточку — наша спека против конкурентов ТОЙ ЖЕ модели (по полю, см. card_issue)
    ws=wb.create_sheet("Проверить карточку")
    _hdr(ws, ["№","Наш товар","Ваша цена","Что не так (спека)","Подтверждение (конкурент)"], st)
    r=1
    for o in sorted(ours, key=lambda o:o["name"]):
        iss=card_issue(o, by_sn.get(o["sn"], []))
        if not iss: continue
        label,ov,v1,nd,ratio,src=iss
        r+=1
        ws.cell(r,1,r-1); ws.cell(r,2,o["name"])
        c3=ws.cell(r,3, o["price"] if o["price"] else "нет цены")
        if o["price"]: c3.number_format="# ##0"
        c3.hyperlink=o["url"]; c3.font=st["blue"]
        ws.cell(r,4, f"{label}: у нас {ov:g}, у конкур. {v1:g} ({nd} сайт.)").font=st["orange"]
        lk=ws.cell(r,5, f"[{src['site']}] {src['name'][:50]}")
        lk.hyperlink=src["url"]; lk.font=st["blue"]
    widths=[5,46,12,40,52]
    for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
    ws.freeze_panes="B2"; ws.auto_filter.ref=f"A1:{get_column_letter(5)}{r}"
    n_chk=r-1
    # 5: Снятые у конкурентов (карточки компрессоров со статусом «снято»)
    ws=wb.create_sheet("Снятые у конкурентов")
    _hdr(ws, ["№","Карточка конкурента (снято)","Сайт","Цена (была)","Серия","У нас (тот же ряд)","Наша цена"], st)
    sny=[c for c in cands if c["status"]=="снято"]
    sny.sort(key=lambda c:(str(c["sn"][0]), c["sn"][1], c["site"]))
    our_by_sn=defaultdict(list)
    for o in ours: our_by_sn[o["sn"]].append(o)
    r=1
    for c in sny:
        r+=1
        ws.cell(r,1,r-1)
        nm=ws.cell(r,2,c["name"][:70]); nm.hyperlink=c["url"]; nm.font=st["strike"]
        ws.cell(r,3,c["site"])
        if c["price"]: pc=ws.cell(r,4,c["price"]); pc.number_format="# ##0"; pc.font=st["strike"]
        ws.cell(r,5, f"{str(c['sn'][0]).upper()}{c['sn'][1]:g}")
        oo=[o for o in our_by_sn.get(c["sn"],[]) if match(o,[c])]
        if oo:
            o=oo[0]; l=ws.cell(r,6,o["name"][:50]); l.hyperlink=o["url"]; l.font=st["blue"]
            if o["price"]: ws.cell(r,7,o["price"]).number_format="# ##0"
        elif c["sn"] in our_sn:
            ws.cell(r,6,"серия есть у нас")
    widths=[5,60,18,12,10,50,12]
    for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
    ws.freeze_panes="B2"; ws.auto_filter.ref=f"A1:{get_column_letter(7)}{r}"
    # 6: Особо проверить — НЕТ СЕРИИ в названии (power-only: kw+bar+fl равны, power_only.py)
    n_po=0
    if po:
        ws=wb.create_sheet("Особо проверить (нет серии)")
        HDR=["№","Наш товар (нет серии в названии)","Ваша цена"]+COMPETITORS+["min конк.","Δ к min, %","Совпало"]
        _hdr(ws, HDR, st); r=1
        for o,m in sorted(po, key=lambda t:t[0]["name"]):
            r+=1; ws.cell(r,1,r-1); ws.cell(r,2,o["name"])
            c3=ws.cell(r,3, o["price"] if o.get("price") else "нет цены")
            if o.get("price"): c3.number_format="# ##0"
            c3.hyperlink=o["url"]; c3.font=st["blue"]
            per=defaultdict(list)
            for c in m: per[c["site"]].append(c)
            prices=[]
            for ci,site in enumerate(COMPETITORS):
                cell=ws.cell(r,4+ci); cs=per.get(site)
                if not cs: cell.fill=st["nomatch"]; continue
                priced=[c for c in cs if c.get("price") and c.get("status")!="снято"]
                show=min(priced,key=lambda c:c["price"]) if priced else cs[0]
                if show.get("price"):
                    cell.value=show["price"]; cell.number_format="# ##0"; cell.hyperlink=show["url"]
                    if show.get("status")=="снято": cell.font=st["strike"]
                    else: cell.font=st["blue"]; prices.append(show["price"])
                else:
                    cell.value="снято" if show.get("status")=="снято" else "По запросу"
                    cell.hyperlink=show["url"]
                    cell.font=st["strike"] if show.get("status")=="снято" else st["blue"]
                if len(cs)>1: cell.fill=st["warn"]
            if prices:
                mn=min(prices); ws.cell(r,10,mn).number_format="# ##0"
                if o.get("price"): ws.cell(r,11, round((o["price"]-mn)/mn*100,1))
            sov=f"{o['kw']:g} кВт / {o['bar']:g} бар / {o['fl']:g} л/мин"
            if o.get("ip"): sov+=f" / IP{o['ip']}"
            ws.cell(r,12,sov)
        widths=[5,46,12]+[13]*6+[11,10,30]
        for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
        ws.freeze_panes="C2"; ws.auto_filter.ref=f"A1:{get_column_letter(len(HDR))}{r}"
        n_po=r-1
    path=os.path.join(OUTDIR, f"{title}_spec_review.xlsx")
    wb.save(path)
    return dict(clean=len(clean), ambig=len(ambig), no=n0, gap=n_gap, chk=n_chk, sny=len(sny),
                po=n_po, path=path)

def build_all(only=None, min_ours=5, min_cands=5):
    os.makedirs(OUTDIR, exist_ok=True)
    ours_all=load_ours_all(); cands_all=load_comp_all()
    from power_only import build_po_pairs       # lazy: модуль импортирует нас же
    po_all=build_po_pairs(set(only) if only else None)
    res={}
    brands=sorted(set(ours_all)&set(cands_all) | set(po_all))
    for b in brands:
        if only and b not in only: continue
        po=po_all.get(b)
        if (len(ours_all.get(b,[]))<min_ours or len(cands_all.get(b,[]))<min_cands) and not po:
            continue                              # po-only бренды (ZUV) собираем всегда
        title=b.capitalize() if b!="ir" else "IngersollRand"
        r=build_brand(b, title, ours_all.get(b,[]), cands_all.get(b,[]), po=po)
        res[b]=r
        print(f"{b:<14} матч {r['clean']:>4} | неодн {r['ambig']:>3} | без {r['no']:>4} | "
              f"GAP {r['gap']:>4} | карточки {r['chk']:>3} | снятые {r['sny']:>4} | "
              f"без серии {r['po']:>3}")
    return res

if __name__=="__main__":
    import sys as _s
    only=set(_s.argv[1:]) or None
    res=build_all(only)
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for b,r in res.items(): z.write(r["path"], os.path.basename(r["path"]))
    print(f"-> {ZIP} ({len(res)} брендов)")
