"""Спек-матчер (прототип, обкатан на Atlas Copco).
Сцепка: бренд + СЕРИЯ+НОМЕР из имени/слага (обязательно) + специи «не противоречат»
(сравниваем только заполненные с обеих сторон; мусор отсеян диапазонами здравого смысла)
+ жёсткие текст-флаги варианта (FF=осушитель, TM/270/500=ресивер, VSD).
Результат: 1 кандидат -> спек-матч; 2-3 -> жёлтый "проверь"; >3 -> не матчим.

Наши специи: specs_compact.csv (Битрикс-выгрузка, колонки IP_PROP*):
  22553 бренд | 22576 серия | 22562 кВт | 22573 бар | 22571 л/мин |
  22583 масл/безмасл | 22586 частотник | 22574 ресивер | 22565 осушитель
Специи конкурентов: specs-JSON из прогонов парсера (CHECKED).
"""
import re

SER_ATLAS = re.compile(
    r'\b(xahs|xrhs|xrvs|xrys|xrxs|xats|xavs|xas|gx|ga|zr|zt|ze|za|le|lf|lt|sf|aq|gv|g)'
    r'\s*[- ]?\s*(\d+[.,]?\d*)', re.I)

def num(x):
    m = re.search(r'\d+[.,]?\d*', str(x))
    return float(m.group().replace(",", ".")) if m else None

def yn(x): return str(x).strip().lower() in ("да","yes","есть","1","true")

def series_num(text, ser_re=SER_ATLAS):
    s = str(text).replace("_"," ").replace("-"," ")
    m = ser_re.search(s)
    if not m: return None
    fam = m.group(1).lower(); n = float(m.group(2).replace(",","."))
    # "+" / "plus" сразу после номера серии = другая модель (GA11 != GA11+)
    tail = s[m.end():m.end()+4].lower()
    if tail.lstrip().startswith("+") or tail.lstrip().startswith("plus") or s[m.start():m.end()].endswith("+"):
        fam = fam + "+"
    # вариант "GA11+ ..." где + прилип к числу
    around = s[max(0,m.start()-1):m.end()+2]
    if "+" in around.replace(fam.rstrip("+"),"",1):
        fam = fam.rstrip("+") + "+"
    return (fam, n)

def text_flags(text):
    """FF (осушитель) / VSD (частотник) / ресивер — вшиты в артикул.
    rv: None = ресивер не упомянут; 1 = упомянут без объёма; N = объём в литрах."""
    tl = " " + str(text).lower().replace("_","-") + " "
    ff  = 1 if re.search(r'\bff\b', tl) else None
    vsd = 1 if ("vsd" in tl or "частот" in tl) else None
    rv = None
    m = re.search(r'(?:ресивер\w*|resiver\w*|receiver\w*|\btm\b)[- ]?(\d{2,3})?', tl)
    if m: rv = num(m.group(1)) or 1
    m2 = re.search(r'[- ](270|500|900)\b', tl)
    if m2: rv = float(m2.group(1))
    return ff, vsd, rv

def receiver_filter(o_rv, cands):
    """Направленное правило ресивера (наш артикул кодирует вариант всегда):
    - у нас ресивера НЕТ -> кандидаты с явным ресивером исключаются;
    - у нас ЕСТЬ -> если есть кандидаты с явным ресивером, молчаливые отбрасываем
      (раз сосед подписан, молчание = без ресивера); объёмы, если оба известны, должны сойтись."""
    if o_rv is None:
        return [c for c in cands if c.get("rv") is None]
    explicit=[c for c in cands if c.get("rv") is not None]
    pool = explicit if explicit else cands
    out=[]
    for c in pool:
        crv=c.get("rv")
        if crv is None or crv==1 or o_rv==1 or crv==o_rv: out.append(c)
    return out

def sane_kw(v):  return v if v and 0.2 <= v <= 2000 else None   # мусор (вес 0.0001) -> None
def sane_bar(v): return v if v and 3 <= v <= 400 else None

def bar_from_text(t):
    m = re.search(r'[- ](\d{1,2}[.,]?\d?)\s*(?:бар|bar|p\b|р\b)', str(t).lower().replace("_","-"))
    return sane_bar(num(m.group(1))) if m else None


def sane_flow(v):
    """Производительность -> л/мин. Если значение <100 — это м³/мин, домножаем."""
    if not v: return None
    if v < 100: v = v*1000
    return v if 100 <= v <= 120000 else None

def agree(a, b):                 # пусто с любой стороны = не противоречит
    return a is None or b is None or a == b

def agree_num(a, b, tol=0.06):
    return a is None or b is None or abs(a-b) <= tol*max(a, b)

FLOW_TOL = 0.04   # допуск производительности (строго: GA11=1560 vs GA11+=1820 не путать)

def match(o, cands):
    """o, cands: dict с ключами sn,kw,bar,oil,vsd,ff,rv. Возврат: список подходящих."""
    out = []
    for c in cands:
        if o["sn"] != c["sn"]: continue
        if not (agree_num(o["kw"], c["kw"]) and agree_num(o["bar"], c["bar"], 0.01)): continue
        if not agree(o["oil"], c["oil"]): continue
        if o.get("vsd",0) != c.get("vsd",0): continue   # жёстко: из текста
        if o.get("ff",0)  != c.get("ff",0):  continue   # жёстко: из текста
        if not agree(o.get("rv"), c.get("rv")): continue
        out.append(c)
    return out

# --- классификатор «это компрессор, а не запчасть/категория» (по имени+слагу) ---
PARTS_RE = re.compile(
    r'фильтр|filter|\bkit\b|ремкомплект|запчаст|сепаратор|separator|клапан|valve|'
    r'ремень|belt|подшипник|прокладк|gasket|картридж|шланг|hose|муфта|радиатор|'
    r'охладител|термостат|манометр|реле|плата|датчик|sensor|контроллер|двигател|'
    r'электродвигател|\bмотор\b|\bблок\b|airend|маслоотделит|\bмасло\b|смазк|antifriz|'
    r'осушител[ья]\s|^осушител|рем\.?\s?набор|to-\d|для компрессор|элемент\b|'
    r'сервис|обслуживан|\bнабор|'
    r'vozdushnyy-filtr|maslyanyy-filtr|remen\b|klapan|podshipnik|separator|filtr|'
    r'dvigatel|kontroller|datchik|mufta|shlang|radiator|ohladitel|termostat|manometr', re.I)

CATEGORY_RE = re.compile(
    r'компрессоры\b|kompressoryi\b|kompressoryi[-_]|[-_]kompressoryi|вся\s+серия|модельный\s+ряд',
    re.I)

def is_compressor(text):
    """text = имя + слаг. Компрессор (товар), а не категория/листинг серии и не запчасть.
    Множественное «компрессорЫ»/«kompressoryi» где угодно = листинг серии -> не товар."""
    s = str(text).lower()
    t = " " + s.replace("_", "-") + " "
    if CATEGORY_RE.search(s) or CATEGORY_RE.search(t): return False   # листинг серии
    if PARTS_RE.search(t): return False
    return ("компрессор" in t or "kompressor" in t or "compressor" in t)
