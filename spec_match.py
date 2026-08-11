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
    r'\b(xahs|xrhs|xrvs|xrys|xrxs|xats|xavs|xams|xaxs|'      # 4-букв. серии (длинные — раньше)
    r'xrs|xah|xas|gx|ga|zr|zt|ze|za|lz|le|lf|lt|sf|aq|gv|'  # 3-2-букв.
    r'xa|u|y|h|x|g)'                                         # 1-2-букв. (короткие — последними)
    r'\s*[- ]?\s*(\d+[.,]?\d*)(l\b)?', re.I)

def num(x):
    m = re.search(r'\d+[.,]?\d*', str(x))
    return float(m.group().replace(",", ".")) if m else None

def yn(x): return str(x).strip().lower() in ("да","yes","есть","1","true")

def series_num(text, ser_re=SER_ATLAS):
    s = str(text).replace("_"," ").replace("-"," ")
    s = re.sub(r'(?i)(copco|копко)(?=[a-zа-яё])', r'\1 ', s)   # «COPCOG 200» -> «COPCO G 200»
    m = ser_re.search(s)
    if not m: return None
    fam = m.group(1).lower(); n = float(m.group(2).replace(",","."))
    if m.group(3): fam += "l"          # суффикс L = low pressure (G15L != G15, GA37L != GA37)
    # "+" / "plus" сразу после номера серии = другая модель (GA11 != GA11+)
    tail = s[m.end():m.end()+4].lower()
    if tail.lstrip().startswith("+") or tail.lstrip().startswith("plus") or s[m.start():m.end()].endswith("+"):
        fam = fam + "+"
    # вариант "GA11+ ..." где + прилип к числу
    around = s[max(0,m.start()-1):m.end()+2]
    if "+" in around.replace(fam.rstrip("+l"),"",1):
        fam = fam.rstrip("+") + "+"
    # v-p-k пишет плюс ПОСЛЕ давления: «GA 18 10 plus» = GA18+ 10 бар. Отдельное слово
    # plus/плюс в остатке = плюс-серия (символ «+» НЕ ловим: «VSD+13FF» — плюс у VSD)
    if not fam.endswith("+") and re.search(r'\bplus\b|\bплюс\b', s[m.end():], re.I):
        fam = fam + "+"
    return (fam, n)

def text_flags(text):
    """FF (осушитель) / VSD (частотник) / ресивер — вшиты в артикул.
    rv: None = ресивер не упомянут; 1 = упомянут без объёма; N = объём в литрах.
    FF часто приклеен к давлению («G 11 10FF», «ga18plus-8-5ff») — \\b между цифрой
    и буквой не срабатывает, ловим отдельно; (?<![a-z]) отсекает англ. 'off'/'staff'."""
    tl = " " + str(text).lower().replace("_","-") + " "
    ff  = 1 if re.search(r'(?<![a-z])ff\b|\dff\b', tl) else None
    # VSD-маркеры имени: заводской код модели надёжнее спек-таблиц, т.к. в таблицах
    # ключ «частот» неоднозначен (Частота тока/вращения/напряжения ≠ частотный привод).
    # Вариации инвертора: VSD / частот / VS (ET) / I-серии ABAC (GENESIS I.15, FORMULA.I,
    # дефисный слаг genesis-i-15) / MEI30 / инвертор. (pnevmoteh поле пишет корректно.)
    vsd = 1 if ("vsd" in tl or "частот" in tl or "инвертор" in tl or "inverter" in tl
                or re.search(r'\bvs\b', tl) or re.search(r'\bi\.\d', tl)
                or re.search(r'(?:genesis|formula)[\s.\-]*i\b', tl)
                or re.search(r'\bmei\d', tl)) else None
    rv = None
    m = re.search(r'(?:ресивер\w*|resiver\w*|receiver\w*|\btm)[- ]?(\d{2,3})?\b', tl)
    if m: rv = num(m.group(1)) or 1
    m2 = re.search(r'[- (/](270|500|900)(?:\s*[лl])?\b', tl)   # «P/500», «(270 л)», «270l»
    if m2: rv = float(m2.group(1))
    # 100/150/200/300 — частые объёмы, но конфликтуют с номерами моделей (GA 200): берём
    # только «хвостовые» (конец имени / перед «л» / закрывающей скобкой): CAVF7.5-10GA-300, 3/200
    m3 = re.search(r'[-(/](100|150|200|300)(?:\s*[лl])?(?=\s*$|\s*\))', tl.rstrip())
    if m3 and rv is None: rv = float(m3.group(1))
    return ff, vsd, rv

def ip_class(text):
    """Класс защиты IP из текста. 54 и 55 — один ценовой класс (нормализуем 55→54);
    23 — другой (открытое исполнение). Нет в тексте — None (молчание ≠ «отличается»)."""
    m = re.search(r'(?<![a-z])ip\s*[- ]?(\d{2})\b', str(text).lower())
    if not m: return None
    v = m.group(1)
    return "54" if v == "55" else v

def ip_filter(o_ip, cands):
    """Направленное правило IP: кандидат отбрасывается ТОЛЬКО если обе стороны явно
    размечены и классы различаются (54≈55 равны). Неразмеченные не фильтруем —
    молчание совместимо с любым исполнением."""
    if not o_ip:
        return cands
    return [c for c in cands if not c.get("ip") or c["ip"] == o_ip]

def cool_class(text, prop=None):
    """Тип охлаждения: 'water'/'air'/None. Сначала проп/спека, потом имя. «вод»/«жидк» =
    water, «возд»/«air» = air. Если в значении ОБА (воздушное/водяное, опционально) или
    масляное/неясное → None (любое, не фильтруем). Голый 'ac' НЕ трактуем (= Atlas Copco)."""
    s = (str(prop) + " " + str(text)).lower()
    water = bool(re.search(r'водян|жидкост|water[\s-]?cool|\bwc\b', s))
    air   = bool(re.search(r'воздушн\w*\s*охлажд|air[\s-]?cool', s))
    # явный проп «воздушное»/«водяное» (значение колонки/ключа целиком)
    pl = str(prop).strip().lower()
    if pl in ("воздушное", "воздушный", "air"): air = True
    if pl in ("водяное", "водяной", "жидкостное", "water"): water = True
    if water and air: return None     # «воздушное/водяное», «опционально» — подходит к любому
    if water: return "water"
    if air:   return "air"
    return None

def cool_filter(o_cool, cands):
    """Направленное правило охлаждения (как ip_filter): отбрасываем кандидата, только
    если обе стороны явно размечены и классы различаются. Молчание = совместимо."""
    if not o_cool:
        return cands
    return [c for c in cands if not c.get("cool") or c["cool"] == o_cool]

def ff_filter(o_ff, cands, o_text=""):
    """Направленное правило FF (как receiver_filter): если в серии конкурент РАЗМЕЧАЕТ
    FF (есть карточки с ff=1) — молчуны при нашем FF отбрасываются (молчание=без осушителя);
    если НИКТО не размечает (Atlas ZR/ZT: FF не пишут) — молчание не противоречит.

    `o_text` — имя нашей карточки, необязательный. Нужен для одного случая: наш проп
    «осушитель» пуст, но в имени стоит та же метка исполнения, что и у кандидата
    (CECCATO DRC 40/10 DRY — строки совпадают побуквенно, а ff у нас не выставлен,
    потому что suffix_flags ловит «Д» только в хвосте). Пустой проп при совпавшей
    метке — молчание, а не «нет осушителя»; сравниваются именно МЕТКИ, поэтому
    правило верно и там, где буквы значат не осушитель (ET SOF Dry = безмасляный)."""
    if not o_ff:                       # наш без FF: явные FF-карточки исключаем
        osig = variant_sig(o_text) if o_text else frozenset()
        return [c for c in cands if not c.get("ff")
                or (osig and variant_sig(f"{c.get('name','')} {c.get('url','')}") == osig)]
    explicit=[c for c in cands if c.get("ff")]
    return explicit if explicit else cands

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

# Буквенные метки ИСПОЛНЕНИЯ в хвосте модели. Что именно они значат — зависит от
# бренда, и проверять надо по данным, а не по здравому смыслу:
#   ARIACOM NT7 DF, Dalgakiran ... ID, FIAC ... DRY, Comprag PORTA DRY = осушитель;
#   Spitzenreiter S-10DF   = ЧАСТОТНИК (9 карточек из 10: проп «Частотный
#                            преобразователь: да», осушителя в спеках нет);
#   ET SOF Dry 110         = безмасляный (сухого сжатия), тоже не осушитель.
# Поэтому метка сравнивается КАК МЕТКА («у нас DF — и у него DF»), а вывод
# «это осушитель» из неё не делается: ff остаётся за пропами и suffix_flags.
VARIANT_MARKS = re.compile(r'(?<![a-zа-яё])(df|id|dry)(?![a-zа-яё])', re.I)

# --- код модели: имя без бренда, маркетинга и электрики ------------------------------------
# Нужен, чтобы метку исполнения считать по СТРОКЕ, а не по хвостовому суффиксу серии.
# Хвостовая схема ловит буквы только сразу за номером и разъезжается на реальных данных:
#   наш «ET SL 45 H AC 10 бар»  vs  их «ET-Compressors SL 45 HAC (IP23) 10» — одно и то же.
# Проверено адверсарно 11.08 на 60 парах: правило «буквенные токены совпали» дало
# 23 совпадения из 23 без единой ложной пары.
_TAIL = re.compile(r"\s(?:[сc]\s+частотн|без\s+муфт|[сc]\s+осушител|[сc]\s+ресивер|на\s+ресивер|"
                   r"в\s+кожух|на\s+раме|на\s+шасси|со\s+склад|в\s+наличии|купить|цена|заказать|"
                   r"доставк|[сc]\s+двигател|и\s+двигател|[сc]\s+прямым|[сc]\s+ременн|"
                   r"[сc]\s+воздушн|[сc]\s+водян|[сc]\s+тормоз)", re.I)
_DESCR = {"винтовой","винтовые","винтовая","поршневой","спиральный","безмасляный","безмаслянный",
          "дизельный","дизельная","электрический","электический","передвижной","стационарный",
          "двухступенчатый","маслозаполненный","компрессор","компрессора","компрессоры",
          "компрессорная","станция","kompressor","vintovoy","vintovoj","screw","compressor",
          "compressors","air","с","c","и","без","на","в","бар","атм","квт","kw","бару","л","мин",
          "кг","мм","гц","hz","серии","серия","модель","новый","new","прямой","ременной","ременный",
          "привод","приводом","бу","б","у","шасси","фикс","высота","высотой","тормоз","тормоза",
          "тормозами","ресивере","ресивером","ресивер","осушителем","осушитель","ce","се","эл",
          "предмет","предметов","набор","комплект"}


def model_code(name, brand):
    """Токены обозначения модели + класс IP. Возврат: (список токенов, '23'|None)."""
    s = str(name or "")
    # Маркетинговый хвост режем ТОЛЬКО после начала обозначения (после первой цифры) —
    # иначе «Винтовой компрессор на ресивере FINI PLUS 15-13-500» обрубается в ноль.
    d = re.search(r"\d", s)
    m = _TAIL.search(s, d.start() if d else 0)
    if m: s = s[:m.start()]
    ip = re.findall(r"(?i)\bip\s*(\d{2})", s)      # достаём ДО снятия скобок: «(IP23)»
    s = re.sub(r"\(.*?\)", " ", s)                 # прочее в скобках — пояснения, не код
    s = re.sub(r"(?i)\bip\s*\d+", " ", s)
    # Электрика («400В 3ф 50Гц») — не обозначение: наш каталог её дописывает, конкуренты нет.
    # Без этого Atlas «G15L 13P/400 3ф 50 Гц» не сходится с их «G 15L 13 P».
    s = re.sub(r"(?i)\d+\s*(?:в|v)\b|\b\d*\s*(?:ф|ph)\b|\b\d*\s*(?:гц|hz)\b", " ", s)
    s = s.lower().replace("_"," ").replace("/"," ").replace("-"," ").replace(","," ")
    from matcher import BRAND_ALIASES
    btoks = {brand} | {a for a, c in BRAND_ALIASES.items() if c == brand}
    out = []
    for t in re.split(r"[^0-9a-zа-яё.+]+", s):
        t = t.strip(".")
        if not t or t in _DESCR or t in btoks: continue
        if re.fullmatch(r"ip\d+", t): continue
        # Буквы и цифры внутри одного токена — разные части обозначения, а пишут их то
        # слитно, то раздельно: «FV7508»=«FV-7508», «ВК340Н»=«ВК340-7,5Н», «GA37L»=«GA 37L».
        for part in re.findall(r"\d+(?:[.,]\d+)?|[a-zа-яё]+\+?", t):
            if part in _DESCR or part in btoks: continue
            out.append(part)
    return out, (ip[0] if ip else None)


def variant_letters(name, brand):
    """Буквенные токены обозначения = метка исполнения. Смысл букв брендозависим
    (W=водяное охлаждение и H AB/AC=винтовой блок у ET, K=осушитель у Renner,
    ID=осушитель у Dalgakiran, DF=частотник у Spitzenreiter, HH=высокое давление
    у Sullair), но сравниваем мы их КАК МЕТКИ с обеих сторон — поэтому словарь
    значений не нужен и ошибиться в трактовке нельзя."""
    # Из метки ВЫЧИТАЕМ всё, чем уже владеют выделенные правила, иначе признак
    # считается дважды и метка расходится на пустом месте. Замер 11.08 по Atlas:
    # 392 разрыва, из них 234 из-за нашей приписки «без N/CE», 126 из-за TM/FM
    # (ресивер — receiver_filter), 27 из-за FF (осушитель — ff_filter), 52 из-за
    # P/Pack (модуль считает Pack/AC/WC одним товаром, см. шапку brand_spec_review).
    from brand_spec_review import _CYR2LAT, _STOPW
    drop = _STOPW | {"n","bar","atm","psi","l","kg","mm","db","hp","kw","cd","dd","yd"}
    toks, _ = model_code(name, brand)
    return frozenset(t.translate(_CYR2LAT) for t in toks
                     if not re.fullmatch(r"[\d.]+", t) and len(t) <= 5
                     and t not in drop and t.translate(_CYR2LAT) not in drop)


def same_variant(a, b):
    """Метки равны как множества ИЛИ как склеенная строка: «H AC» == «HAC»."""
    glue = lambda s: "".join(sorted("".join(sorted(s))))
    return a == b or glue(a) == glue(b)


def variant_sig(text):
    """Метки исполнения из имени/артикула: чем эта карточка отличается от «голой».

    Производители кодируют исполнение буквой в хвосте модели, и это ФИЗИЧЕСКИ
    другой аппарат, а не то же самое дешевле. Проверено адверсарно 11.08 на
    50 сцепках: 5 из 8 ложных матчей — ровно этот случай, разница по массе
    +45…+540 кг:
        Dalgakiran INVERSYS PLUS 55-13 ID  vs  55-13     (1830 vs 1290 кг)
        ALMiG BELT 18/13                   vs  18/13-O   (410 vs 505 кг)
        ALMiG FLEX-7/8 R                   vs  FLEX-7/8-O R (265 vs 310 кг)
        ARIACOM NT7 13DF 500               vs  NT7 500   (420 vs 370 кг)

    Метки: `ID`, `DF`, `DRY`, `-O`, атласовский `FF` — отдельным токеном либо
    приклеенные к числу («13DF»). Одиночная «o» — латинская и только как
    самостоятельный токен, иначе поймаем пол-каталога.
    """
    t = " " + str(text).lower().replace("_", "-") + " "
    t = re.sub(r"(\d)([a-zа-я]+)", r"\1 \2", t)     # 13df -> 13 df
    toks = set(re.split(r"[^a-zа-я0-9]+", t))
    sig = set()
    if toks & {"id", "df", "dry", "o"} or re.search(r'(?<![a-z])ff\b', t):
        sig.add("exec")
    return frozenset(sig)


def prefer_exact_variant(o_text, cands):
    """Из кандидатов одной серии предпочесть тех, чьё исполнение совпадает с нашим.

    Ключевое наблюдение проверки: в 6 ложных матчах из 8 верная карточка лежала
    у ТОГО ЖЕ конкурента отдельным SKU — то есть ошибочный кандидат не просто
    лишний, он ВЫТЕСНИЛ верного. Поэтому это не резак, а предпочтение: если
    среди кандидатов есть хоть один с тем же набором меток, оставляем только
    таких; если точного нет — возвращаем всё как было. Потерять совпадение
    правило не может по построению, а консервативность фильтров не трогает.
    """
    if not cands:
        return cands
    ours = variant_sig(o_text)
    exact = [c for c in cands
             if variant_sig(f"{c.get('name','')} {c.get('url','')}") == ours]
    return exact if exact else cands


def sane_kw(v):  return v if v and 0.2 <= v <= 2000 else None   # мусор (вес 0.0001) -> None
def sane_bar(v): return v if v and 3 <= v <= 400 else None

def bar_from_text(t):
    m = re.search(r'[- ](\d{1,2}[.,]?\d?)\s*(?:бар|bar|p\b|р\b)', str(t).lower().replace("_","-"))
    return sane_bar(num(m.group(1))) if m else None



def flow_to_lmin(v, key=""):
    """Производительность -> л/мин, с учётом единицы из ключа.
    'м3/мин'/'м³/мин' -> *1000; 'м3/час' -> /60*1000=/0.06; 'л/мин' как есть.
    Без единицы: <60 трактуем как м³/мин (1.82->1820). Защита: отсекаем нереальное
    (<60 л/мин невозможно для компрессора; так давление 8/10/9.75 в поле не пройдёт)."""
    if v is None: return None
    kl = str(key).lower()
    if "м3/час" in kl or "м³/час" in kl or "m3/h" in kl: v = v/0.06
    elif "м3" in kl or "м³" in kl: v = v*1000
    elif "л/мин" in kl or "l/min" in kl: pass
    elif v < 60: v = v*1000        # без единицы и маленькое = м³/мин
    return v if 100 <= v <= 120000 else None

def sane_flow(v, key=""):   # совместимость
    return flow_to_lmin(v, key)

def is_flow_key(k):
    """True, если ключ спеки = производительность (а не «Производитель»/«Производство»/
    «Гарантия производителя»/«Страна производства» — все ловятся подстрокой «произв»).
    Мягкий перенос \\xad внутри «Производитель­ность» убираем."""
    kl = str(k).lower().replace("\xad", "")
    if "гарант" in kl or "код производ" in kl or "производител" in kl and "производительн" not in kl:
        return False
    if "пропускн" in kl or "производительн" in kl:
        return True
    return "произв" in kl and any(u in kl for u in ("л/мин", "м3", "м³", "нм3", "куб", "/час", "/мин"))

def flow_value(raw, key=""):
    """Из значения производительности (возможно диапазон '8,2 - 21,9') берём МАКСИМУМ
    и приводим к л/мин по единице ключа. Так VSD (диапазон) сравнивается по максимуму,
    как и прописано в каталоге prokompressor."""
    s = str(raw)
    nums = re.findall(r'\d+[.,]?\d*', s.replace(" ", ""))
    if not nums: return None
    vals = [float(x.replace(",", ".")) for x in nums]
    return flow_to_lmin(max(vals), key)

def bar_value(raw):
    """Давление из значения, возможно диапазон ('4 бар – 13 бар' у VSD) -> МАКСИМУМ
    (номинал модели = верх диапазона, как «GA37 ...13FF» и в каталоге prokompressor).
    Санити-фильтр (3..400 бар) отсекает приклеенные объёмы/мусор."""
    vals = [sane_bar(float(x.replace(",", "."))) for x in re.findall(r'\d+[.,]?\d*', str(raw))]
    vals = [v for v in vals if v]
    return max(vals) if vals else None

def bar_flow_pairs(raw_bar, raw_flow, flow_key=""):
    """Сдвоенные карточки конкурентов («8/10» давление + «1,665/1,435» произв.) = ПЕРЕЧЕНЬ
    вариантов на одной странице -> по паре (8,1665),(10,1435), каждая матчит свой SKU.
    «/» = варианты; ' - ' = непрерывный VSD-диапазон (берём max, как раньше). Иначе одна пара."""
    sb=str(raw_bar or "")
    if re.search(r'\d\s*/\s*\d', sb) and "-" not in sb and "–" not in sb:
        bars=[b for b in (sane_bar(num(x)) for x in sb.split("/")) if b]
        if len(bars)>=2:
            sf=str(raw_flow or "")
            flows=[flow_to_lmin(num(x), flow_key) for x in sf.split("/")] if re.search(r'\d\s*/\s*\d', sf) else []
            return [(b, flows[i] if i<len(flows) else flow_value(sf, flow_key)) for i,b in enumerate(bars)]
    return [(bar_value(raw_bar), flow_value(raw_flow, flow_key))]


def agree(a, b):                 # пусто с любой стороны = не противоречит
    return a is None or b is None or a == b

def agree_num(a, b, tol=0.06):
    return a is None or b is None or abs(a-b) <= tol*max(a, b)

FLOW_TOL = 0.04   # допуск производительности (строго: GA11=1560 vs GA11+=1820 не путать)

CHECK_FIELDS=[("кВт","kw",0.06),("бар","bar",0.10),("произв","fl",0.04)]

def card_issue(o, same, fields=CHECK_FIELDS):
    """«Проверить карточку»: поле нашего товара расходится с конкурентами ТОЙ ЖЕ модели.
    Проверяем ПО ПОЛЮ (не по факту матча): пиннуем остальные поля + FF/VSD; если наше
    значение никто из вариантов не подтверждает, а на ДРУГОМ значении согласны ≥2 сайта —
    флаг. Так не прячется ×10-ошибка, сматченная к карточке без этого поля (Atmos ST45 flow
    680 vs 6600), и не флагуется верное значение, подтверждённое карточкой (ZR75 9 бар)."""
    for label,key,tol in fields:
        ov=o.get(key)
        if not ov: continue
        others=[(k2,t2) for (l2,k2,t2) in fields if k2!=key]
        variant=[c for c in same if c.get(key)
                 and (c.get("ff") or 0)==(o.get("ff") or 0) and (c.get("vsd") or 0)==(o.get("vsd") or 0)
                 and all(o.get(k2) and c.get(k2) and abs(o[k2]-c[k2])<=t2*max(o[k2],c[k2]) for k2,t2 in others)]
        if not variant: continue
        if any(abs(c[key]-ov)<=tol*max(c[key],ov) for c in variant): continue   # наше значение подтверждено
        for c1 in variant:
            v1=c1[key]; doms={c2["site"] for c2 in variant if abs(c2[key]-v1)<=tol*max(c2[key],v1)}
            if len(doms)>=2:
                src=next(c2 for c2 in variant if abs(c2[key]-v1)<=tol*max(c2[key],v1))
                return (label, ov, v1, len(doms), max(ov,v1)/min(ov,v1), src)
    return None

def match(o, cands):
    """o, cands: dict с ключами sn,kw,bar,fl,oil,vsd,ff,rv. Возврат: список подходящих.
    Масло НЕ сравниваем: серия+номер обязаны совпасть, а внутри одной модели масляность
    не варьируется — конфликт значит враньё на сайте (compressortyt: XAS97 «безмасляный»).
    Бар 3%: v-p-k пишет рабочее давление (6.9 при номинале 7), а 7 vs 7.5 (6.7%) режется."""
    out = []
    for c in cands:
        if o["sn"] != c["sn"]: continue
        if not (agree_num(o["kw"], c["kw"]) and agree_num(o["bar"], c["bar"], 0.03)): continue
        if not agree_num(o.get("fl"), c.get("fl"), FLOW_TOL): continue   # производительность 4%
        # частотник (+35% к цене): есть=есть, нет=нет — режем только ЯВНЫЙ конфликт.
        # Тристейт: 1=знаем-да (имя/проп/спек-ключ), 0=знаем-нет (проп/спек «нет»),
        # None=не указано (матчится с пометкой «частотник не подтверждён» в отчёте).
        ov, cv = o.get("vsd"), c.get("vsd")
        if ov is not None and cv is not None and ov != cv: continue
        # привод: только если ИЗВЕСТЕН с обеих сторон (наш — проп Битрикса, их — спека/схема
        # имени Berg). Молчание совместимо. Ловит ВК-18.5Р (ремен) vs ВК-18.5 (прямой).
        if o.get("dr") and c.get("dr") and o["dr"] != c["dr"]: continue
        if (o.get("ff") or 0)==1 and (c.get("ff") or 0)==1: pass        # оба размечены FF — ок
        elif (o.get("ff") or 0)!=(c.get("ff") or 0) and (c.get("ff") or 0)==1:
            # У конкурента осушитель, у нас проп пустой — обычно это конфликт. Но если в
            # НАШЕМ имени стоит ТА ЖЕ метка исполнения, что и у него, пустой проп — молчание,
            # а не «нет осушителя»: suffix_flags знает только «Д» в хвосте и «с осушителем»,
            # а «DRY» в середине не ловит. Замер 11.08: 22 пары Ceccato, где имена совпадают
            # ПОБУКВЕННО («CECCATO DRC 40/10 DRY CEC A MEAA»), резались именно тут.
            osig = variant_sig(o.get("name", ""))
            if not osig or osig != variant_sig(f"{c.get('name','')} {c.get('url','')}"):
                continue        # метки нет или она другая — правило работает как раньше
        # наш ff=1 vs их None — решает ff_filter (направленно по серии)
        if not agree(o.get("rv"), c.get("rv")): continue
        out.append(c)
    return out

# --- классификатор «это компрессор, а не запчасть/категория» (по имени+слагу) ---
PARTS_RE = re.compile(
    r'фильтр|filter|\bkit\b|ремкомплект|запчаст|сепаратор|separator|клапан|valve|'
    r'ремень\b|belt|подшипник|прокладк|gasket|картридж|шланг|hose|муфта|радиатор|'
    r'охладител|термостат|манометр|реле|плата|датчик|sensor|контроллер|двигател|'
    r'шкив|shkiv|\bремонт|\bremont\b|'
    r'электродвигател|\bмотор\b|\bблок\b|airend|маслоотделит|\bмасло\b|масломинеральн|'
    r'маслосинтетич|paroil|\broil\b|смазк|antifriz|'
    r'осушител[ья]\s|^осушител|рем\.?\s?набор|\bto-\d|для компрессор|элемент\b|'
    r'сервис|обслуживан|\bнабор|'
    r'vozdushnyy-filtr|maslyanyy-filtr|remen\b|klapan|podshipnik|separator|filtr|'
    r'dvigatel|kontroller|datchik|mufta|shlang|radiator|ohladitel|termostat|manometr|'
    r'servis|obsluzhivan|remkomplekt|zapchast|\bnabor|komplekt|\bmaslo\b|maslomineral', re.I)

CATEGORY_RE = re.compile(
    r'компрессоры\b|kompressor(?:yi|y|i)(?![a-z])|вся\s+серия|модельный\s+ряд',
    re.I)

# «[тип] компрессор …» в начале названия = это компрессор-ЮНИТ (а не запчасть «X компрессора»).
# Тогда part-слова (belt/двигатель/блок/осушитель) — это атрибуты/комплектация, а не товар-часть.
UNIT_RE = re.compile(
    r'^[\s\d.,№"]*([a-zа-яё]+\s+){0,2}'
    r'(винтов|спиральн|поршнев|роторн|дизельн|передвижн|центробежн|зубчат|безмасл|турбо|'
    r'двухступенч|одноступенч|scroll|screw|spiral|piston|rotary)\w*[\s-]+'
    r'(компрессор|kompressor|compressor)(?![а-яёa-z])'
    r'|^[\s\d.,№"]*(компрессор|kompressor|compressor)(?![а-яёa-z])', re.I)

def is_compressor(text):
    """text = имя + слаг. Компрессор (товар), а не категория/листинг серии и не запчасть.
    Множественное «компрессорЫ»/«kompressoryi» где угодно = листинг серии -> не товар.
    «[тип] компрессор …» в начале = ЮНИТ (belt/двигатель/осушитель в названии = атрибут)."""
    s = str(text).lower()
    t = " " + s.replace("_", "-") + " "
    if CATEGORY_RE.search(s) or CATEGORY_RE.search(t): return False   # листинг серии
    if UNIT_RE.search(s): return True                                # «винтовой компрессор …» = юнит
    if PARTS_RE.search(t): return False
    return ("компрессор" in t or "kompressor" in t or "compressor" in t)
