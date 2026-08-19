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
                        ip_filter, ip_class, _ip_open, cool_filter, cool_class, is_flow_key, card_issue,
                        prefer_exact_variant, VARIANT_MARKS, model_code,
                        variant_letters, same_variant, VSD_MARK, dim_value,
                        FAMILY_WEIGHT)
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

# Раздел каталога в URL как подсказка классификатору. Часть площадок пишет в имени
# только код модели: у compressortyt 766 карточек Berg из 770 названы «ВК-4Р 8 (IP54)»,
# слова «компрессор» нет ни в имени, ни в слаге — и is_compressor их отбрасывал, хотя
# лежат они в /stanciya/kompr/vintovye/berg/.
#
# Целиком путь в классификатор отдавать НЕЛЬЗЯ: CATEGORY_RE считает листингом всё, где
# есть «компрессорЫ» во множественном числе, а у pnevmo-sklad этот кусок стоит в КАЖДОМ
# товарном адресе (/oborudovanie/vintovye_kompressory/...) — весь сайт стал бы «листингом».
# Поэтому отдаём одно слово в единственном числе, и только когда раздел действительно
# компрессорный. Запчасти это не пропускает: PARTS_RE по-прежнему смотрит на имя и слаг
# («zapchasti», «filtr», «maslo» ловятся там же, где ловились).
_CAT_COMPR = re.compile(r"/(?:kompr|kompressor\w*|kompressori|vintovye|porshnevye|spiralnye|"
                        r"peredvizhnye|dozhimnoj|centrobejnye)(?:/|$)", re.I)
_CAT_PARTS = re.compile(r"/(?:zapchast\w*|raskhodnik\w*|filtry|maslo|servis|obsluzhivan\w*)(?:/|$)", re.I)


def cat_hint(url: str) -> str:
    """«компрессор » если адрес лежит в компрессорном разделе, иначе пусто."""
    path = "/" + "/".join(str(url).split("/")[3:])
    if _CAT_PARTS.search(path):
        return ""
    return "компрессор " if _CAT_COMPR.search(path) else ""


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
# Кириллические одиночки, которые НИКОГДА не бывают именем серии: предлоги и союзы из
# маркетинговой части имени. «а» сюда не входит — это реальная серия Atom (А-11).
_CYR_PREP = {"с","и","в","у","о","к","я","на","до","от","по","за","не","со","из"}

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
    # Класс защиты — не номер модели. У pnevmoteh хвост URL «...-22-kvt-ip23» давал
    # ('kvtip', 23), номер 23 доезжал до ключа даже после подмены серии в base_family,
    # и ATOM А-22Е уходил в ('a', 23) против нашего ('a', 22) — бренд не матчился вовсе
    # (0 из 86 карточек). model_code это уже снимает, gen_series — нет.
    s=re.sub(r"(?i)\bip\s*[- ]?\d+"," ",s)
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
    for m in re.finditer(r"\b([a-zа-я])\s*[- ]?\s*(\d+(?:[.,]\d+)?)"
                         r"\s*([a-z]{1,4}(?![a-zа-я]))?(?!\d)", s):
        w=m.group(1)
        if w in btoks or (re.match(r"[а-я]", w) and w in _CYR_PREP): continue
        suf=m.group(3) if (m.group(3) and m.group(3) not in UNITS and m.group(3) not in _STOPW) else ""
        return ((w+suf).translate(_CYR2LAT), float(m.group(2).replace(",",".")))
    return None

# Режим ВКЛЮЧЁН (откат: VARIANT_STRICT=0). Буквы уходят из ключа семейства в метку
# исполнения, которая сравнивается отдельно (см. variant_filter). Замер 11.08 на
# боевых данных, после вычитания из метки признаков, которыми владеют выделенные фильтры:
#   ложных сцепок снято     7 из 8 доказанных
#   верных пар возвращено  24 из 32 подтверждённых агентами
#   ложных притащено        1 из 27 опровергнутых
#   верных потеряно         4 из 42 ранее подтверждённых
#   всего разорвано 815 пар
# Все 30 разрывов проверены агентами по живым страницам: 12 полезных (сцепка была
# ложной), 5 ошибочных, 10 к этому моменту уже починены нормализацией, 3 ложные пары
# режим не тронул. То есть 71% состоявшихся разрывов — по делу.
#
# ОТРИЦАТЕЛЬНЫЙ РЕЗУЛЬТАТ (проверено, не берём). Попытка добить оставшиеся 5 ошибочных
# разрывов сделала хуже по всем осям, поэтому откачена:
#   снимать бренд-слова всего справочника  -> разрывы 12/7, сматчено 9 169 (было 9 475)
#   не фильтровать предлоги в base_family  -> разрывы 12/8, сматчено 9 127
# Оставшиеся 5 — реальная асимметрия источников: FINI VISION 1513-500F-ES (конкурент
# пишет F, мы нет), Ekomak EKO 18G CR STD, Ceccato DRE 120/13 A CE, BERG ATOM (бренд
# кириллицей у конкурента), Comaro MD-P 132 l/8 против их «132-08 I».
# Выключение (вернуться к прежнему поведению): VARIANT_STRICT=0.
#
# ОТРИЦАТЕЛЬНЫЙ РЕЗУЛЬТАТ 12.08 — две попытки ослабить ff_filter, обе не берём:
#   ff считать ПО САЙТУ («молчание v-p-k ≠ нет осушителя, он это слово вообще не
#     пишет»)                          -> пар +1 139, но наш «VEGA 18 PLUS 10
#     (с осушителем)» сцепляется с v-p-k «VEGA 18 10», хотя PLUS у них лежит рядом
#     отдельной карточкой. ff — единственное, что их сейчас разводит;
#   поблажка «молчун с той же меткой не режется» -> сматчено +2, но неоднозначных
#     373 -> 523: та же беда, «plus» в метку не попадает (стоп-слово).
#   сравнивать метки ТОЛЬКО по буквам, которыми конкурент реально пользуется
#     («500D» против их «Compact» — асимметрия источников) -> сматчено 11 562
#     (+328!), но старые наборы валятся по трём осям из четырёх: ложных снято
#     5/8 вместо 7/8, ложных притащено 4/27 вместо 1/27, верных потеряно 5/42.
#     Прирост куплен возвратом уже доказанных ложных сцепок — не берём.
#
# ОТМЕНЁННЫЙ ОТРИЦАТЕЛЬНЫЙ РЕЗУЛЬТАТ. Сначала «plus» в метке исполнения был записан
# сюда же как «не берём»: 16 наших карточек теряли матч целиком, и я счёл, что
# конкурент слово просто не пишет. Проверка агентами по живым страницам это
# ОПРОВЕРГЛА — «plus» взят (см. drop в spec_match.variant_letters):
#   Lupamat: обе версии лежат на одной площадке отдельными карточками, DHK PREMIUM
#     55 710 л/мин и 9 500 кг против DHK PLUS 51 530 и 8 700 — два независимых
#     источника (compressortyt, pnevmo-sklad) дают одни и те же числа. 3 отсева верны;
#   Kraftmann/ALMiG: снято 334 пары «PLUS против не-PLUS» — это ровно тот класс
#     ошибки, который агенты поймали дважды (VEGA 15 R -> VEGA 15 PLUS R, 335 кг
#     против 390; BELT 37/13 -> Belt 37 Plus, 590 против 660).
# ЦЕНА, известная и не закрытая: 13 карточек Dalgakiran INVERSYS PLUS ...-500D
# матч теряют, и это ОШИБОЧНЫЙ отсев — завод (dalgakiran.ru) приравнивает индекс
# 11-10-500D к имени «INVERSYS Plus 11 Compact», а конкурент пишет Compact. Метка
# расходится на нашей букве D (осушитель), которой и так владеет ff_filter. Вычитать
# её из метки пробовал: неоднозначных 399 -> 430 и минус ещё одна верная пара, хуже.
# Правильное лечение — нормализовать «500D» -> Compact для Dalgakiran, отдельной задачей.
VARIANT_STRICT = os.getenv("VARIANT_STRICT", "1").strip() not in ("0", "false", "no")


# BOGE склеивает букву исполнения с буквой серии: наш «C 4 D 10» и их «CD 4-10» — один
# SKU (вес 210 кг грамм в грамм, как и у C 9 D / CD 9, C 3 L / CL 3, C 4 LDR / CLD 4 —
# семь пар проверены агентом по живым карточкам, D = рефрижераторный осушитель). Из-за
# склейки семейства расходились ('c',4) против ('cd',4), пулы не пересекались, и сравнение
# даже не начиналось: 36 из 92 карточек конкурента висели на этом.
_BOGE_GLUE=re.compile(r"^([cs])([dflr]{1,3})$")


def boge_split(tok):
    """«cd» -> ('c','d'), «sldf» -> ('s','ldf'); не-склейка возвращается как есть."""
    m=_BOGE_GLUE.match(tok)
    return (m.group(1), m.group(2)) if m else (tok, "")


def base_family(text, brand):
    """Семейство без буквенного хвоста: первый буквенный токен кода модели + номер.
       наш «ET SL 45 H AC 10 бар»               -> ('sl', 45)
       их  «ET-Compressors SL 45 HAC (IP23) 10» -> ('sl', 45)"""
    sn = gen_series(text, brand)
    if not sn: return sn
    # Предлоги отсекаем ТЕМ ЖЕ списком, что и fallback gen_series, иначе два места
    # расходятся: у compressortyt имя вида «Винтовой компрессор С прямым приводом
    # Harrison HRS-9510000», и одиночное «с» становилось именем семейства — 130 наших
    # карточек уезжали в ('с', …) вместо ('hrs', …) и не сходились ни с чем.
    # Имя бренда, написанное кириллицей, тоже отсекаем: model_code сверяет токен с
    # brand/aliases ДО транслитерации, поэтому «АТОМ А-22» у aerocompressors давало
    # семейство ('atom', 22) против нашего ('a', 22) — та же поломка бренда, что и с IP.
    btoks={brand}|{a for a,c in BRAND_ALIASES.items() if c==brand}
    for t in model_code(text, brand)[0]:
        if re.fullmatch(r"[\d.]+", t) or t in _CYR_PREP or t.translate(_CYR2LAT) in btoks:
            continue
        t=t.translate(_CYR2LAT)
        if brand=="boge": t=boge_split(t)[0]
        return (t, sn[1])
    return sn


def ser_of(text, brand):
    if brand=="atlas": return series_num(text)
    if VARIANT_STRICT: return base_family(text, brand)
    return _with_variant(gen_series(text, brand), text)


def variant_filter(o_name, cands, brand, o_vsd=None):
    """Направленное правило исполнения — в форме receiver_filter/ff_filter.

    Если среди кандидатов есть карточка с ТОЙ ЖЕ меткой — оставляем только такие.
    Если нет, а метки у кандидатов вообще проставлены — значит конкурент исполнения
    различает, а нашего не возит: режем. Если ни у кого меток нет — молчание,
    совместимо. Работает только при VARIANT_STRICT."""
    ours = variant_letters(o_name, brand)
    cname = lambda c: c.get("name") or slug(c["url"]).replace("_"," ")
    marks = [(c, variant_letters(cname(c), brand)) for c in cands]
    exact = [c for c, m in marks if same_variant(m, ours)]
    if exact: return exact
    if ours and any(m for _, m in marks):
        keep = vsd_mark_fallback(o_name, cands, brand, o_vsd)
        return keep or alien_letter_fallback(ours, marks, brand)
    return cands


_OUR_ALPHA = {}
SKU_LETTER = {}


def alien_letter_fallback(ours_marks, marks, brand):
    """ФОЛБЭК: метка конкурента = наша плюс буквы, которых НЕТ НИ В ОДНОЙ нашей карточке.

    Такая буква не может различать НАШИ исполнения — мы этот признак вообще не кодируем,
    значит для нас это молчание, а не конфликт. Работает на буквах-шуме: OZEN «EN 11 TD
    500 л.» (литры), MARK «MSS-45A/10 380V3PH50HZ» (напряжение) — 18 пар держатся на этом.

    Но «нет у нас» не значит «ничего не значит у них»: проверка агентами 18.08 по живым
    карточкам вскрыла 9 ложных пар KraftMachine, где хвост «АВ» = винтовой блок Hanbell AB
    вместо базового AC и стоит на 31-38% дороже (KM11-10 пВ 272 809 против пВ АВ 377 800).
    Туда же ушёл наш собственный пример из прошлой версии этого комментария — ET «Z»:
    у compressortyt рядом с Z-карточкой лежит не-Z с ценой копейка в копейку нашей, то
    есть Z у них тоже отдельный SKU, и обоснование было ошибочным (вывод отозван).

    Поэтому буквы, которыми конкурент САМ разводит два своих SKU одного ключа на одной
    площадке (SKU_LETTER), из фолбэка исключены. Замер: убирает 20 пар, из них 12
    доказанно ложные (KraftMachine AB/AV, «MAS+» — морское исполнение Atlas, ARIACOM
    «winter pack», DALGAKIRAN Eagle-H)."""
    alpha = _OUR_ALPHA.get(brand)
    if not alpha: return []
    theirs = SKU_LETTER.get(brand) or frozenset()
    out = []
    for c, m in marks:
        extra = m - ours_marks
        if extra and (m & ours_marks) == ours_marks and not (extra & alpha) and not (extra & theirs):
            out.append(c)
    return out


def exec_weight_pick(o, cands, brand):
    """Молчаливый по исполнению кандидат достаётся ОДНОЙ нашей карточке — по массе.

    Та же логика, что у OUR_BARE: если мы сами держим исполнения отдельными SKU, а
    конкурент — одной карточкой, отдавать её всем нашим нельзя. Замер 19.08: 175 карточек
    конкурентов размазаны по нескольким нашим исполнениям (ЗИФ 111, ARIACOM 24, Atlas 15,
    Chicago 15, Atmos 6) — это 196 заведомо лишних пар, потому что верной может быть
    только одна.

    Разводит масса, и она у этих исполнений честная: ARIACOM SAX 110 «на шасси» весит
    1650 кг против 1400 у стационарного, и карточка конкурента с 1400 кг — это ровно
    стационар, а с 1680 — шасси. Проверено: в 124 группах из 175 массы наших исполнений
    различаются, в остальных 51 совпадают или пусты — там молчание, режем не мы.

    Правило срабатывает ТОЛЬКО когда у конкурента признака исполнения нет вовсе: если он
    его пишет, работает exec_filter, а он строже и точнее."""
    if not cands: return cands
    ot={k:v for k,v in exec_tags(o.get("name","")).items() if k in _EXEC_KEYS}
    if not ot: return cands
    sibs=[(t,w) for t,w in OUR_EXEC.get((brand, o["sn"]), ())
          if t and w and t!=tuple(sorted(ot.items()))]
    if not sibs: return cands
    mine=o.get("we")
    # ОТРИЦАТЕЛЬНЫЙ РЕЗУЛЬТАТ 19.08 (проверено, откачено): гасить правило, когда наш вес
    # стоит у нескольких моделей бренда («константа, а не измерение»). Цель была спасти
    # 13 карточек с копипастом веса — Chicago Pneumatic пишет 650 кг семи типоразмерам
    # CPS 90G..160. Но у ЗИФ веса честно повторяются между типоразмерами (ПВ-16/1,0 и
    # ПВ-18/1,0 оба 3800 кг), и правило гасло там, где резало верно: на проверочном
    # наборе «разр2» возвращались 2 доказанно ложные пары при пороге и 2, и 3 модели.
    # Те 13 карточек — не дефект правила, а битый вес в нашей выгрузке.
    if not mine: return cands
    others=[w for _,w in sibs if abs(w-mine)/max(w,mine) > 0.01]   # реально другая масса
    if not others: return cands
    out=[]
    for c in cands:
        ct=exec_tags((c.get("name") or "")+" "+(c.get("mnt") or ""))
        cw=c.get("we")
        if any(k in ct for k in _EXEC_KEYS) or not cw:
            out.append(c); continue                    # исполнение размечено или веса нет
        if abs(cw-mine) <= min(abs(cw-w) for w in others): out.append(c)
    return out


def learn_family_weights(cands):
    """Вес, который продавец ставит нескольким моделям бренда сразу, — не доказательство.
    Таких (площадка, бренд, вес) 4 475 из 17 357: Comaro LB 5,5-10/270 и LB 7,5-10/270
    оба 400 кг, ЗИФ ПВ-16/1,6 и ПВ-18/1,6 оба 3800 кг. См. weight_confirms."""
    seen = defaultdict(set)
    for b, lst in cands.items():
        for c in lst:
            if c.get("we"): seen[(c["site"], b, round(c["we"]))].add(c["sn"])
    FAMILY_WEIGHT.clear()
    FAMILY_WEIGHT.update(k for k, v in seen.items() if len(v) > 1)


def learn_sku_letters(cands):
    """Буквы, которыми конкурент разводит СВОИ SKU: тот же ключ серия+номер, та же
    площадка, у одной карточки метка = метка другой плюс буква. Раз он держит обе
    карточки отдельно — буква несёт исполнение, а не написание."""
    SKU_LETTER.clear()
    for b, lst in cands.items():
        by = defaultdict(list)
        for c in lst: by[(c["sn"], c["site"])].append(c)
        acc = set()
        for grp in by.values():
            if len(grp) < 2: continue
            ms = [variant_letters(c.get("name") or slug(c["url"]).replace("_"," "), b) for c in grp]
            for a in ms:
                for d in ms:
                    if a is d or not a: continue
                    if (a & d) == d and (a - d): acc |= (a - d)
        if acc: SKU_LETTER[b] = acc


def vsd_mark_fallback(o_name, cands, brand, o_vsd):
    """ФОЛБЭК: метки разошлись только буквой частотника, а сам частотник совпал явно.

    Заводы кодируют частотник то буквой в индексе, то словом: наш «Hansmann RS11E» и
    «BERG ВК-160 Е» против их «RS11A VSD» и «ВК-160 16 с частотником». Буква известна из
    нашего же каталога (learn_vsd_marks), но вычитать её из метки ВСЕГДА нельзя — замер
    18.08: +57 матчей ценой 6 подтверждённых верных пар на проверочных наборах и роста
    неоднозначных с 379 до 879, потому что без буквы сливаются соседние исполнения.

    Поэтому послабление действует только когда старый путь не дал НИЧЕГО и обе стороны
    ЯВНО размечены одинаковым частотником — тогда признак уже доказан полем vsd, а метка
    считала бы его второй раз."""
    if o_vsd is None or not VSD_MARK.get(brand): return []
    ours = variant_letters(o_name, brand, drop_vsd=True)
    out = []
    for c in cands:
        if c.get("vsd") != o_vsd: continue
        nm = c.get("name") or slug(c["url"]).replace("_"," ")
        if same_variant(variant_letters(nm, brand, drop_vsd=True), ours): out.append(c)
    return out


# Привод из ИМЕНИ — запасной источник, когда проп/спека молчат: aerocompressors
# зовёт карточку «ЗИФ-СВЭ-1,0/1,0 ШМ ременная» без ключа привода в таблице, и наша
# прямоприводная ШМ сцеплялась с их ременной (доказанная ложная пара 12.08, вес 155
# против 290 кг). «\bремен» — граница слова, иначе ловится «соВРЕМЕНная»; «прямой»
# берём только в связке со словом «привод», иначе поймается «прямой пуск» (DOL).
_DR_BELT=re.compile(r"\bремен|\bремень|\bремнём", re.I)
_DR_DIRECT=re.compile(r"прям(?:ой|ым)[\s(]+привод|привод\W{0,3}прямой", re.I)
def dr_name(n):
    t=str(n or "")
    if _DR_BELT.search(t): return "ремен"
    if _DR_DIRECT.search(t): return "прямой"
    return None


# Марка дизельного двигателя: заводы дают разные SKU под разные моторы (ЗИФ-ПВ-16/0,7
# на Д-260 ММЗ и на ЯМЗ — отдельные позиции с разными ценами, доказано 13.08). Д-2хх —
# двигатели Минского моторного завода. Извлекается из имени и из спек/пропов.
_ENG=(("ямз","ямз"),("cummins","cummins"),("камминз","cummins"),("deutz","deutz"),
      ("дойц","deutz"),("kubota","kubota"),("yanmar","yanmar"),("perkins","perkins"),
      ("caterpillar","cat"),("weichai","weichai"),("isuzu","isuzu"),
      ("yamz","ямз"),("mmz","ммз"),("ммз","ммз"),("минск","ммз"))
_ENG_D2=re.compile(r"д-?2\d\d", re.I)
_ENG_CAT=re.compile(r"cat", re.I)
def eng_make(*texts):
    t=" ".join(str(x or "") for x in texts).lower()
    for pat,mk in _ENG:
        if pat in t: return mk
    if _ENG_D2.search(t): return "ммз"
    if _ENG_CAT.search(t): return "cat"
    return None


# «стационарн» = класс неподвижных вместе с «на раме»: compressortyt зовёт бесшассийные
# Atmos «Стационарный компрессор PDP 20» прямо в H1 — 6 доказанных ложных пар 13.08
# против наших «на шасси». Порядок значим: «салазки» раньше «стационарн» — их
# «стационарный на салазках» должен давать тег салазок, как и наша «на салазках».
_EXEC_MNT=(("салазк","салазки"),("подвес","подвес"),("на скатах","скаты"),
           ("без шасси","рама"),("на раме","рама"),("стационарн","стационар"),("шасси","шасси"))
# «стационарный» у Atmos — это ЕГО «на салазках»/«на раме» (h1 «Стационарный компрессор
# PDP 20», в описании «шасси на салазках»): конфликтует только с «шасси», с прочими
# неподвижными совместим. Доказано 13.08: 6 ложных пар стационар-vs-шасси и 2 верные
# стационар-vs-салазки.
_MNT_OK={frozenset({"стационар","рама"}), frozenset({"стационар","салазки"}),
         frozenset({"стационар","подвес"}), frozenset({"стационар","скаты"})}
def exec_tags(name):
    """Исполнение станции из русских слов имени. Проверка разрывов цен 12.08: из 27
    доказанных ложных пар 15 — ЗИФ, и почти все различаются именно этими словами
    (подвес/скаты/салазки, кожух/без кожуха, пакет «Север», шасси/рама).
      кожух и монтаж — двусторонние теги: режем только при явном конфликте обеих сторон
      («в кожухе» против «без кожуха»; «на скатах» против «с подвесом»);
      «север» — по наличию слова: зимний пакет в имя ставят ВСЕГДА, когда он есть
      (никто не пишет «без пакета Север»), поэтому отсутствие слова = базовая версия.
    «на раме» = «без шасси» — один вариант (доказано живой парой ЗИФ-ПВ-20/1,2)."""
    t=" "+str(name).lower()+" "
    tags={"север": "да" if "север" in t else "нет"}
    if re.search(r"без\s+кожух", t): tags["кожух"]="нет"
    elif "кожух" in t: tags["кожух"]="да"
    for pat,val in _EXEC_MNT:
        if pat in t: tags["монтаж"]=val; break
    # Тормоз шасси — двусторонний тег: Atmos продаёт «фикс. с тормозом» и «фикс. без
    # тормозов» отдельными SKU с разницей ~240 тыс (2 доказанные ложные пары 13.08).
    if re.search(r"без\s+тормоз", t): tags["тормоз"]="нет"
    elif "тормоз" in t: tags["тормоз"]="да"
    # Сеть 60 Гц — по наличию: в РФ стандарт 50 Гц, «60 Hz» пишут только на экспортных
    # исполнениях (Atlas ZR 90 - 9 60 Hz FF — другой двигатель, доказано 13.08).
    tags["сеть"]="60" if re.search(r"\b60\s*(?:hz|гц)", t) else "50"
    # Конфигурация модулей спиральных станций: «DS 15-10 (4x3.7)» и «(2x7,5)» — физически
    # разные машины при одинаковой суммарной мощности (веса 695 vs 440 кг, доказано 12.08).
    m=re.search(r"\((\d)\s*[xх×]\s*(\d+(?:[.,]\d+)?)\)", t)
    if m: tags["модули"]=f"{m.group(1)}x{m.group(2).replace(',','.')}"
    # Высота дышла у шассийных Atmos: «рег. высота» и «фиксированная» — отдельные SKU
    # (PDP15 7: доказанная ложная пара 13.08). Двусторонний тег.
    # «изменяемая высота буксира» — то же самое словами aerocompressors.
    if re.search(r"\bрег\w*[.\s]+высот|регулируем|изменяем\w*\s+высот", t): tags["дышло"]="рег"
    elif re.search(r"\bфикс", t): tags["дышло"]="фикс"
    # Заводская схема Atmos в артикуле: A = регулируемая высота буксира, F = фиксированная,
    # B = с тормозом, U = без тормоза; SKID = на салазках, BOX = в кожухе. Расшифровка снята
    # агентами 18.08 с pnevmo-sklad («PDP20 10 F/U», поле «Комплектация: Фиксированный без
    # тормоза») и подтверждена ценами: у одного продавца на PDP 15-7 живут ШЕСТЬ карточек —
    # F/U 1 926 485, A/U 1 991 681, F/B 2 008 496, A/B 2 104 668, SKID 1 923 142, Box
    # 1 883 807 руб. Три наши шассийные карточки без этого сцеплялись с одной чужой.
    m=re.search(r"\b([af])\s*/\s*([ub])\b", t)
    if m:
        tags["дышло"]="рег" if m.group(1)=="a" else "фикс"
        tags["тормоз"]="да" if m.group(2)=="b" else "нет"
    if re.search(r"\bskid\b", t): tags["монтаж"]="салазки"
    elif re.search(r"\bbox\b", t): tags["кожух"]="да"
    # Диапазон давления в скобках: Renner RSF 355 DW-13 продаётся как «(6-13 бар)» и
    # «(13-15 бар)» — отдельные SKU у конкурента (доказано 13.08). Двусторонний тег.
    m=re.search(r"\((\d+(?:[.,]\d+)?)\s*[-–]\s*(\d+(?:[.,]\d+)?)\s*бар\)", t)
    if m: tags["диапазон"]=f"{m.group(1)}-{m.group(2)}".replace(",",".")
    return tags


def exec_filter(o_name, cands):
    """Направленный фильтр исполнения станции (см. exec_tags). У кандидата к имени
    приклеивается спека «Исполнение»: rutector пишет «На раме / Стационарный» именно
    там, а имя оставляет чистым — наш «ЗИФ ПВ-16/1,0 (на шасси)» иначе не отличить."""
    ot=exec_tags(o_name)
    out=[]
    for c in cands:
        ct=exec_tags((c.get("name") or "")+" "+(c.get("mnt") or ""))
        bad=False
        for k in ("кожух","монтаж","север","тормоз","сеть","модули","дышло","диапазон"):
            if k not in ot or k not in ct or ot[k]==ct[k]: continue
            if k=="монтаж" and frozenset({ot[k],ct[k]}) in _MNT_OK: continue
            if k=="диапазон":
                # Вложенный диапазон — не другой SKU: Renner RSF 200 D-13 «(6-13)» входит
                # в «(6-15)» (объединённая карточка). Режем только НЕвложенные: «(6-13)»
                # против «(13-15)» — доказанные отдельные позиции.
                (a1,a2),(b1,b2)=(map(float,ot[k].split("-")), map(float,ct[k].split("-")))
                if (a1>=b1 and a2<=b2) or (b1>=a1 and b2<=a2): continue
            bad=True; break
        if not bad: out.append(c)
    return out


# (бренд, серия+номер), где наш каталог держит БЕЗресиверную карточку. Заполняется
# в load_ours_all; нужен receiver-правилу ниже. Той же природы OUR_IPBASE (есть наша
# карточка с открытым/неуказанным IP) и OUR_NOVSD (есть наша без частотника): если мы
# сами различаем исполнения, молчаливый кандидат принадлежит базовой версии.
OUR_BARE=set(); OUR_IPBASE={}; OUR_NOVSD=set()
# (бренд, серия+номер) -> [(теги исполнения, масса)] по НАШИМ карточкам. Нужен правилу
# exec_weight_pick ниже. Заполняется в load_ours_all.
OUR_EXEC=defaultdict(list)
_EXEC_KEYS=("дышло","тормоз","монтаж","кожух")
_IPWORD=re.compile(r"[,(]?\s*ip\s*-?\s*\d{2}\)?", re.I)
# «AC» + серия Atlas Copco в начале обозначения = Atlas Copco. Одиночное «AC» в общий
# распознаватель брендов не добавить (это же «air cooled»: у нас 116 карточек вида
# «ET SL 11 H AC 10 бар»), поэтому связка с серией: 154 карточки, из них 151 с
# производителем SOUAIR — марки, которой нет ни у одного из четырёх конкурентов.
_AC_ATLAS=re.compile(r"(^|\s)AC\s+(ZR|ZT|ZE|ZA|GA|GX|GV|LZ|LE|LF|LT|SF|AQ|XA[A-Z]{0,2})\s*-?\s*\d", re.I)
def _noip(name):
    """Имя без IP-метки — для сравнения «отличаются ли карточки только классом защиты»."""
    return re.sub(r"\s+", " ", _IPWORD.sub(" ", str(name).lower())).strip()


def pick_cands(o, pool, brand):
    """Канонический порядок отбора кандидатов. Порядок ЗНАЧИМ, менять нельзя без замера.

    метка исполнения -> ресивер -> осушитель, от самого твёрдого признака к самому мягкому:
      * метка (ES/VS/PM/DF) стоит в заводском артикуле с обеих сторон — это факт;
      * ресивер наш артикул кодирует всегда («K-MAX 1513-500 ES» = 500 л);
      * ff — проп и слова в имени, размечен неровно от площадки к площадке.
    Раньше ff шёл первым и выбивал верную пару чужой разметкой с ДРУГОГО сайта:
    наш «FINI K-MAX 38-08 ES» уходил к pnevmoteh «38-08 ES VS» (там в имени написано
    «с осушителем»), а молчаливые v-p-k/aerocompressors «38-08 ES» вылетали до того,
    как метка VS успевала их развести. Замер 11.08: перестановка даёт +35 наших
    карточек с матчем и +1 028 пар, ни одна карточка матч не теряет.
    """
    m = cool_filter(o.get("cool"), ip_filter(o.get("ip"), match(o, pool), o))
    m = exec_filter(o.get("name", ""), m)
    m = (variant_filter(o.get("name", ""), m, brand, o.get("vsd")) if VARIANT_STRICT
         else prefer_exact_variant(o.get("name", ""), m))
    m = receiver_filter(o.get("rv"), m)
    # Наш артикул кодирует ресивер всегда. Если у нашей карточки объём, а в каталоге
    # РЯДОМ лежит безресиверная той же серии (мы сами различаем оба SKU) — молчаливый
    # кандидат принадлежит базовой версии, а не нашей: aerocompressors «RSA 5.5-10»
    # без ключа ресивера уже верно сцеплен с нашей «RSA 5.5 10 бар», но заодно ложно
    # доставался нашей «RSA 5.5-10-500» (проверка разрывов цен 12.08, 2 пары).
    rv=o.get("rv")
    if rv and rv!=1 and (brand, o["sn"]) in OUR_BARE:
        m=[c for c in m if c.get("rv") is not None]
    m=exec_weight_pick(o, m, brand)
    # Привод: режем только явный конфликт обеих сторон (проп/спека, потом имя). На всех
    # данных 13.08 это ровно ОДНА пара — та самая доказанная ложная ЗИФ ШМ «ременная»;
    # в 21 495 парах привод совпадает, 6 711 односторонних молчаливо совместимы.
    od=o.get("dr") or dr_name(o.get("name"))
    if od:
        m=[c for c in m if c.get("dr_weak")
           or (c.get("dr") or dr_name(c.get("name"))) in (None, od)]
    # Марка дизеля — направленно: обе стороны знают и различаются -> разные SKU
    # (ЗИФ-ПВ на ММЗ и на ЯМЗ — отдельные позиции, доказано 13.08).
    if o.get("eng"):
        m=[c for c in m if c.get("eng") in (None, o["eng"])]
    # Наш каталог сам различает исполнения -> молчаливый кандидат принадлежит базовой
    # версии (образец — ресиверное правило выше). Доказано 13.08: их безымянный
    # KM11-13рВ (292 кг) достался нашей «KM11-13рВ, IP54», хотя наша базовая IP23
    # лежит рядом; их HRS-9513800 без VSD — нашей VSD-версии при нашей базовой рядом.
    # ...и только когда карточки отличаются ИСКЛЮЧИТЕЛЬНО меткой IP: наша «KM11-13рВ,
    # IP54» против нашей же базовой «KM11-13рВ». Harrison «HRS-941300T3» (IP54) при базе
    # «HRS-941300» (IP23) сюда не попадает — их безымянная T3-карточка не базовая, её
    # уже выбрала метка исполнения (замер 13.08: широкий вариант рвал эту верную пару).
    if (o.get("ip") and not _ip_open(o["ip"])
            and _noip(o.get("name")) in OUR_IPBASE.get((brand, o["sn"]), ())):
        m=[c for c in m if c.get("ip") is not None]
    # ОТРИЦАТЕЛЬНЫЙ РЕЗУЛЬТАТ 13.08 (проверено, откачено): то же правило для VSD
    # («наша с частотником + у нас есть базовая -> молчаливых режем») стоило 11
    # подтверждённых верных пар по четырём сотням: конкуренты слово VSD в спеках
    # пишут неровно (Atlas GA-VSD, Boge C), и молчание там — не «базовая версия».
    return ff_filter(o.get("ff"), m, o.get("name", ""))

_DTAIL=re.compile(r'[\d\)лl]\s*[-–]?\s*([дd])\s*(?:\(.*)?$', re.I)   # «270L D», «500Д», «10Д (с осуш.)»
_VSTAIL=re.compile(r'(?:\d|\))\s*(вс|bc)\s*$', re.I)                  # «ВК100Р-10ВС»
_OTAIL=re.compile(r'/[oо][w2]?\s*$', re.I)                            # Zammer «…-500/O», /OW, /O2 = осушитель
# Dali: хвостовое «-F» = частотный преобразователь (Schneider Electric — так пишет наша
# же карточка в блоке «Модификации»). У 41 нашей карточки из 132 с этим суффиксом проп
# частотника стоял «нет», и матчер цеплял нашу F-версию к их безчастотной: 4 ложные пары
# из 40 в адверсарной проверке 18.08 — EN-250/8 II-F, EN-185/7 II-F, EN-200/10 II-F,
# EN-315/8 II-F. Вес и габариты тут не спасают: корпус один, отличается комплектация,
# а цена не-F карточки совпадает с ценой конкурента до рубля.
_FTAIL=re.compile(r'[\d)\sII]\s*[-–]\s*f\s*$', re.I)
def suffix_flags(name, brand, ff, rv, vsd=None):
    """Хвостовые маркеры НЕ-Atlas брендов (у Atlas 'Dd'=дизель, не трогаем):
    Д/D после числа/л = осушитель; ВС = воздухосборник (ресивер упомянут);
    у Dali «-F» = частотник (см. _FTAIL — там же почему он перебивает проп)."""
    if brand=="atlas": return ff, rv, vsd
    nm=str(name).strip()
    if ff is None and (_DTAIL.search(nm) or _OTAIL.search(nm) or "с осушителем" in nm.lower()): ff=1
    if rv is None and _VSTAIL.search(nm): rv=1
    if brand=="dali" and _FTAIL.search(nm): vsd=1
    return ff, rv, vsd

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

# Класс защиты двигателя в спек-таблицах. Ключ у каждой площадки свой, значение — «IP54»,
# «IP 55», иногда «IP21 (IP23)». Признак заводской: у CrossAir, Hansmann и Berg исполнения
# IP23 и IP54/55 продаются отдельными SKU с разницей в цене 5-20%, и агенты по живым
# страницам поймали на этом 6 ложных пар из 11 в выборке 12.08. Пары «IP21 (IP23)» у
# pnevmoteh (1 163 карточки) ПРОПУСКАЕМ: два числа в одном поле — источник сам не уверен,
# а ip_filter режет, и ошибка тут дороже пропуска.
_IPKEY = re.compile(r"(?:класс|степень)\s+защит|\bip\b\s*$|,\s*ip\s*$|^ip\s+", re.I)
_IPVAL = re.compile(r"^\s*(?:ip\s*[- ]?)?(\d{2})\s*$", re.I)


def ip_from_specs(d):
    """Класс защиты из спек-таблицы: '54'/'23'/None. 55 нормализуется к 54 (ip_class).

    Голое число берём только когда сам КЛЮЧ говорит про IP («Класс защиты
    электрооборудования, IP = 54» у pnevmoteh) — иначе поймали бы «Класс защиты: 2».
    """
    for k, v in (d or {}).items():
        if not _IPKEY.search(k.strip()):
            continue
        m = _IPVAL.match(str(v))
        if m:
            return ip_class("ip" + m.group(1))
    return None


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
    # Класс защиты, три источника в порядке надёжности: имя карточки -> проверенная
    # колонка выгрузки -> скрейп нашего сайта. Без него ip_filter молчит с нашей стороны
    # и не может отсечь чужое исполнение: у CrossAir, Hansmann и Berg IP23 и IP54/55 —
    # разные SKU с разницей в цене 5-20%, агенты поймали на этом 6 ложных пар из 11 (12.08).
    ourip={}
    for u,d in load_universe()[1].items():
        if "prokompressor.ru" not in u: continue
        v=ip_class(str(d.get("IP электродвигателя") or ""))
        if v: ourip[u.rstrip("/").split("/")[-1].lower()]=v
    # Колонку выгрузки ищем по ЗНАЧЕНИЮ вида «IP54», а не по номеру свойства: ID у IP_PROP
    # меняется от выгрузки к выгрузке. Свойств об одном ДВА, и доверять можно не всякому:
    # колонку берём, только если она сходится со скрейпом нашего же сайта там, где известны
    # оба значения (>=50 карточек, >=95%). 22674 «IP электродвигателя» проходит — 602/602;
    # 22959 «Степень защиты двигателя» не допущен: арбитраж 12.08 по 18 живым конфликтам
    # дал 15 ложных отсевов из 18 (вес грамм в грамм, цена до рубля — та же машина), в 9
    # виновата наша сторона, в 3 поле расходится даже с нашей же страницей. Подробный
    # разбор и список на правку данных — PROPS_BITRIX.md.
    ipcol=Counter()
    for r in rows.values():
        for k,v in r.items():
            if k.startswith("IP_PROP") and re.fullmatch(r"\s*ip\s*[- ]?\d{2}\s*", str(v), re.I):
                ipcol[k]+=1
    ipcols=[]; ipopen=[]
    for k,_ in ipcol.most_common():
        both=[(v, ourip[code.lower()]) for code,r in rows.items()
              if (v:=ip_class(r.get(k,""))) and code.lower() in ourip]
        if len(both)>=50 and sum(v==s for v,s in both)>=0.95*len(both):
            ipcols.append(k)
        else:
            # У непроверяемой колонки берём ТОЛЬКО открытые значения (IP2x). Асимметрия
            # доказана двумя проверками 12.08: 22959 врёт исключительно значением 54/55 —
            # шаблонным (IP55 стоит на 9 790 карточках из 15 026, Magnus/ЗИФ/DAS/Cross),
            # а редкое осознанное «IP23» (1 662) оказалось верным во всех восьми живых
            # разборах (Berg x3, KraftMachine x2, CrossAir x2, ET). Единственный известный
            # контрпример — KM18,5-13рВ (наш IP23 ошибочен) — принятая цена: 8 верных
            # отсевов против 1 ложного.
            ipopen.append(k)
    OUR_BARE.clear(); OUR_IPBASE.clear(); OUR_NOVSD.clear(); OUR_EXEC.clear()   # OUR_IPBASE: (b,sn) -> имена открытых карточек без IP-метки
    ours=defaultdict(list)
    for code,r in rows.items():
        man=(r.get("IP_PROP22553") or "").strip()
        name=r.get("IE_NAME","")
        b=brand_from_text(man) or BRAND_ALIASES.get(man.lower().split()[0] if man else "", None)
        # Проп не распознан вовсе — не «другой бренд», а отсутствие сведений: тогда имя
        # единственный источник. Ловит 151 карточку безмасляных Atlas Copco, заведённых
        # как «AC ZR 110 FF 10» с производителем SOUAIR (такой марки нет ни у одного из
        # четырёх конкурентов, а карточек серий ZR/ZT у них 336) — до этой правки они не
        # доходили до матчера вообще. Правило узкое: имя пробуется ТОЛЬКО когда проп
        # молчит; спорить с заполненным пропом по-прежнему нельзя (см. mig/airrus ниже).
        b=b or brand_from_text(name) or ("atlas" if _AC_ATLAS.search(name) else None)
        if not b: continue
        # Один завод — две торговые марки. В поле «Производитель» у нас стоит «РКЗ»
        # (Рязанский компрессорный завод), и все 136 карточек AIRRUS уезжали в бренд mig,
        # тогда как конкуренты держат 148 карточек под маркой AIRRUS — бренды не
        # пересекались, и товар не матчился вовсе. Правило узкое, по улике: доверяем имени
        # ТОЛЬКО когда завод известен как выпускающий обе марки. Расширять до «имя всегда
        # важнее пропа» нельзя — сломается «BERG ATOM А-11Е» (проп atom верен, имя врёт),
        # «WIS 40V A» (проп ekomak) и «SIGMA PET AIR» (проп kaeser).
        if b=="mig" and brand_from_text(name)=="airrus": b="airrus"
        if not is_compressor(name+" "+code): continue
        sn=ser_of(name+" "+code, b)
        if not sn: continue
        ff,vsd,rv = text_flags(name+" "+code)
        ff,rv,vsd = suffix_flags(name, b, ff, rv, vsd)
        if rv is None:
            if num(r.get("IP_PROP22564")): rv=num(r.get("IP_PROP22564"))
            elif str(r.get("IP_PROP22574","")).strip().lower() in ("да","есть"): rv=1
        fl = flow_value(r.get("IP_PROP22571"), "л/мин") or flow_value(r.get("IP_PROP22658"), "м3/мин")
        fl = fix_flow_scale(fl, sane_kw(num(r.get("IP_PROP22562"))))
        nm,url,p = price.get(code.lower(), (name, f"https://prokompressor.ru/catalog/{code}/", None))
        wev=num(r.get("IP_PROP22555")); drv=(r.get("IP_PROP22601") or "").strip().lower() or None
        if drv: drv="ремен" if "ремен" in drv else ("прямой" if "прям" in drv else None)
        if str(r.get("IP_PROP22565","")).strip().lower()=="да": ff=1   # проп «осушитель» (направл. флаг — безопасно)
        cl=cool_class(name+" "+code, r.get("IP_PROP22669"))
        # W-хвост после числа = водяное охлаждение — ТОЛЬКО у брендов с доказанным
        # значением буквы (dalgakiran: живая страница «IMPETUS W — с водяным охлаждением»;
        # ET: проверка 11.08). У Enger/Kraftmann/Spitzenreiter W не расшифрован — не трогаем.
        if cl is None and b in ("dalgakiran","et") and re.search(r"\d\s?w\b", name, re.I):
            cl="water"
        pv=str(r.get("IP_PROP22586","")).strip().lower()   # проп «частотник»: знание да/нет
        if vsd is None and pv:                             # имя-маркер приоритетнее пропа
            vsd = 1 if pv=="да" else (0 if pv=="нет" else None)
        oip=(ip_class(name+" "+code)
             or next((v for k in ipcols if (v:=ip_class(r.get(k,"")))), None)
             or ourip.get(code.lower())
             or next((v for k in ipopen
                      if (v:=ip_class(r.get(k,""))) and _ip_open(v)), None))
        # Базой считается только ЯВНО открытая карточка (IP2x): «ip неизвестен» — не база
        # (17,5 тыс. наших карточек без ip делали правило тотальным и рвали верные пары).
        if oip is not None and _ip_open(oip): OUR_IPBASE.setdefault((b, sn), set()).add(_noip(name))
        et={k:v for k,v in exec_tags(name).items() if k in _EXEC_KEYS}
        if et: OUR_EXEC[(b, sn)].append((tuple(sorted(et.items())),
                                         wev if wev and 1<=wev<=50000 else None))
        if rv in (None, 0): OUR_BARE.add((b, sn))
        if not vsd: OUR_NOVSD.add((b, sn))
        ours[b].append(dict(brand=b, sn=sn, kw=sane_kw(num(r.get("IP_PROP22562"))),
                            bar=bar_value(r.get("IP_PROP22573")) or bar_from_text(name+" "+code),
                            fl=fl, oil=oil_of(r.get("IP_PROP22583")), ff=ff, vsd=vsd, rv=rv,
                            name=nm or name, url=url, price=p,
                            ip=oip,
                            cool=cl, eng=eng_make(name, code, *r.values()),
                            we=(wev if wev and 1<=wev<=50000 else None), dr=drv,
                            dim=dim_value(r.get("IP_PROP22556"), "мм")))
    unglue_code(ours)
    learn_vsd_marks(ours)
    _OUR_ALPHA.clear()
    for b, lst in ours.items():        # алфавит меток нашего каталога — см. alien_letter_fallback
        _OUR_ALPHA[b] = set().union(*(variant_letters(o["name"], b) for o in lst)) if lst else set()
    return ours


def fix_flow_scale(fl, kw):
    """Производительность, записанная в м3/мин с множителем 100 вместо 1000.

    В свойстве 22571 «л/мин» у части каталога лежат сотни: ATMOS ST 110 Vario/13 — 1310
    вместо 13100, ST 75/8,5 — 1200 вместо 12000, AIRMAN PDSF830S-W — 2350 вместо 23500.
    Агенты 18.08 сверили 15 таких карточек с паспортами заводов и с НАШЕЙ ЖЕ живой
    страницей: страница показывает верное число, расходится только выгрузка. Всего 207
    карточек, из них 172 ATMOS — 47% бренда; ATMOS ST 90/7,5 из-за этого не матчился
    ни с одной из четырёх площадок, хотя карточка есть у всех.

    Критерий физический, а не брендовый: винтовой компрессор даёт 100-170 литров в
    минуту на киловатт, ниже 40 не бывает ни у одного исполнения. Правим, только если
    умножение на 10 возвращает значение в разумный коридор — иначе оставляем как есть
    (мало ли что за машина)."""
    if not (fl and kw) or fl / kw >= 40: return fl
    return fl * 10 if 40 <= fl * 10 / kw <= 260 else fl


def unglue_code(cat):
    """Слитный код «кВт+бар» в номере серии свести к одному киловатту.

    Заводы пишут одну и ту же модель двумя способами: наш «FINI K-MAX 1110 ES VS» и их
    «K-MAX 11-10 ES VS», наш «Comprag FR1108-270» и их «FR-11 (270л) - 8 бар». В первом
    случае номер серии выходит 1110, во втором 11 — ключ не совпадает, и пара не
    рассматривается вовсе. Затронуто 186 наших карточек: comprag 124, fini 62.

    Разбираем ТОЛЬКО когда число сходится точно: номер == кВт*100 + бар. Так «1110» при
    11 кВт и 10 бар раскладывается, а модель, у которой 1110 — это собственный индекс
    (или литры в минуту), остаётся нетронутой. Схлопывание 1108 и 1110 в один ключь
    безопасно: давление после этого сравнивается отдельным полем и разводит их обратно."""
    for lst in cat.values():
        for o in lst:
            n = o["sn"][1] if o.get("sn") else None
            kw, bar = o.get("kw"), o.get("bar")
            if not (n and kw and bar and n > 100): continue
            if abs(n - (kw * 100 + bar)) < 0.51:
                o["sn"] = (o["sn"][0], kw)


def learn_vsd_marks(ours, min_votes=6):
    """Выучить по НАШЕМУ каталогу буквы метки, которыми бренд кодирует частотник.

    Заводы пишут частотник по-разному: у нас «Hansmann RS11E» и «BERG ВК-160 Е», у
    конкурента то же самое — «RS11A VSD» и «ВК-160 16 с частотником». Метка исполнения
    расходится, и verный матч рвался, хотя признак уже сравнивается отдельным тристейтом
    vsd — то есть считался ДВАЖДЫ (та же причина, по которой из метки вычтены FF, TM/FM
    и Pack, см. variant_letters).

    Букву берём не из словаря, а из улики: внутри одной нашей модели (серия+номер+кВт+бар)
    лежат две карточки, метки которых отличаются РОВНО на одну букву, и та, у которой эта
    буква есть, размечена частотником. Тогда буква и есть кодировка. Замер 18.08: 130
    подтверждений у berg «e», 170 у ariacom «v», 116 у enger «pm», 100 у remeza «вс»,
    78 у atmos «vario». Порог в 6 голосов отсекает случайные совпадения (ironmac «df» — 2).

    Atlas «vsd+» правило не ломает: плюс-версия теряет метку, но остаётся с vsd=1 против
    vsd=0 у базовой, а от простой VSD-версии её по-прежнему отделяет метка «vsd»."""
    votes = defaultdict(Counter)
    for b, lst in ours.items():
        grp = defaultdict(list)
        for o in lst:
            grp[(o["sn"], o.get("kw"), o.get("bar"))].append(o)
        for g in grp.values():
            for i, x in enumerate(g):
                for y in g[i+1:]:
                    if x.get("vsd") is None or y.get("vsd") is None or x["vsd"] == y["vsd"]:
                        continue
                    # Буква должна различать ТОЛЬКО частотник. Если вместе с ним разъезжается
                    # и осушитель, она несёт два смысла сразу: у ATMOS «FD» — это фильтр плюс
                    # осушитель, и вычитание такой буквы склеило нашу ST 55 Vario+ FD/13
                    # (осушитель да, 1290 кг) с их ST 55 Vario+ без FD (осушителя нет, 1230 кг)
                    # — доказанная ложная пара в адверсарной проверке 18.08. То же у ARIACOM,
                    # где «VD» = частотник + осушитель против «V» = только частотник.
                    if (x.get("ff") or 0) != (y.get("ff") or 0): continue
                    mx = variant_letters(x["name"], b); my = variant_letters(y["name"], b)
                    diff = (mx - my) | (my - mx)
                    if len(diff) != 1: continue
                    letter = next(iter(diff))
                    if letter in variant_letters((x if x["vsd"] == 1 else y)["name"], b):
                        votes[b][letter] += 1
    VSD_MARK.clear()
    for b, c in votes.items():
        marks = {t for t, n in c.items() if n >= min_votes}
        if marks: VSD_MARK[b] = marks

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
            # «Цена по запросу» в СВЕЖЕМ прогоне ГАСИТ цену из старых: до фиксов парсера
            # (aerocompressors 11f4a6a, compressortyt 043e9b2) такие карточки получали цену
            # чужого товара из блока похожих — 560 карточек только на aero. Правило
            # «поздний непустой переписывает» само по себе эту грязь не вымоет: перескрейп
            # отдаёт price пустым, и отравленное значение жило бы вечно.
            if (r.get("price_on_request") or "").strip()=="1": price.pop(u, None)
            st=(r.get("series_status") or "").lower()
            if "снят" in st and "v-p-k.ru/catalog" not in u: status[u]="снято"
    cands=defaultdict(list)
    for u,nm in names.items():
        if dm(u) not in COMPETITORS or not is_product_url(u): continue
        b=brand_of(u, nm)
        if not b: continue
        text=(nm or "")+" "+slug(u)
        if not any(ch.isdigit() for ch in text) or not is_compressor(cat_hint(u)+text): continue
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
        ff,rv,vsd = suffix_flags(nm or slug(u), b, ff, rv, vsd)
        bsuf = berg_suffix(text) if b=="berg" else None
        if bsuf is not None:           # заводская схема ВК: код модели ПОЛНЫЙ, отсутствие
            # буквы = знаем-нет (не «не указано»). НО схему перебивает прямое слово в имени:
            # compressortyt называет карточку «ВК-45 7 с частотником» — буквы Е в коде нет,
            # а частотник есть, и старый безусловный override гасил найденный text_flags
            # признак в 0. Тогда наш ВК-45 7 IP23 без частотника сцеплялся с их частотным
            # исполнением (цена выше на 48%) — проверено агентом по живой странице.
            if not (vsd and re.search(r"частотн|инвертор|vsd", text, re.I)):
                vsd = 1 if "e" in bsuf else 0
            if "o" in bsuf: ff=1
        we=dr=sku2=dim=None; lwh={}
        for k,v in d.items():
            kl=k.lower()
            if we is None and ("вес" in kl or "масса" in kl) and "кг" not in str(v).lower()[:0]:
                n=num(v)
                if n and 1<=n<=50000: we=n
            # Габариты: сначала одной строкой («габариты», «габариты (дхшхв), мм»,
            # «габаритные размеры, см» — 60 тыс. карточек), иначе собираем из трёх
            # отдельных ключей длина/ширина/высота (ещё 21 тыс., так пишет pnevmoteh).
            # «присоединительный размер» исключён: там дюймы резьбы, а не корпус.
            if dim is None and "габарит" in kl and "присоед" not in kl: dim=dim_value(v, kl)
            for nm_,ax in (("длина","l"),("ширина","w"),("высота","h")):
                if kl.startswith(nm_) and ax not in lwh:
                    n=num(v)
                    if n: lwh[ax]=n
            if dr is None and "привод" in kl:
                vl=str(v).lower()
                dr="ремен" if "ремен" in vl else ("прямой" if "прям" in vl else None)
            if sku2 is None and "артикул" in kl: sku2=str(v).strip()
            # Спек-ключ «осушитель». Значение пишут по-разному, и «да/есть» — не весь набор:
            # v-p-k ставит «С осушителем = с осушителем» на 2 354 карточках, и без этой
            # ветки их ff оставался пустым. Из-за этого наш VEGA 15 R 270-10 (без осушителя)
            # сцеплялся с их VEGA 15 PLUS R 270 10 (с осушителем, 390 кг против наших 335),
            # хотя верная карточка лежит у них рядом — проверено агентом по живым страницам.
            # «Тип осушителя: адсорбционный» пропускаем: это характеристика уже имеющегося.
            if ff is None and "осушит" in kl and not kl.startswith("тип"):
                vl=str(v).strip().lower()
                if vl in ("да","есть","yes") or vl.startswith("с осушител"): ff=1
            if vsd is None and "частот" in kl:     # «Частотный преобразователь: да/нет»;
                vl=str(v).strip().lower()          # «Частота тока: 50» отсеется значением
                if vl in ("да","есть","yes") or "частотн" in vl: vsd=1
                elif vl in ("нет","no"): vsd=0
        if bsuf is not None:   # ВК-схема Berg: Р в коде = ременный, отсутствие = прямой
            dr = "ремен" if "r" in bsuf else "прямой"
        # «Исполнение» — основной ключ (37 157 карточек), но pnevmo-sklad зовёт то же поле
        # «Комплектация» (1 653) и пишет туда «на шасси»; без него наши шассийные Atmos
        # сравнивались с его карточками вслепую (3 ложные пары, проверка агентами 18.08).
        # Артикул нужен там же: у него исполнение закодировано в нём — «PDP20 10 F/U».
        mnt=next((str(v) for k,v in d.items()
                  if k.strip().lower() in ("исполнение","комплектация")), None)
        mnt=" ".join(x for x in (mnt, sku2) if x) or None
        engsrc=[v for k,v in d.items()
                if "двигат" in k.lower() and "элект" not in k.lower() and "защит" not in k.lower()]
        ceng=eng_make(nm, slug(u), *engsrc)
        srv=None
        for k,v in d.items():
            if "ресивер" in k.lower():
                n=num(v)
                if n and n>=10: srv=n; break
                vl=str(v).strip().lower()
                if vl in ("да","есть","yes") and srv is None: srv=1
                # Явное «нет» — это НЕ «не указано» (тристейт, правило №4). pnevmo-sklad
                # пишет «Ресивер: нет», и без нуля наша RSA 15-12-500 (ресивер 500 л,
                # 450 кг) сцеплялась с их RSA 15 12 без ресивера (290 кг) — 5 доказанных
                # ложных пар Hansmann в проверке разрывов цен 12.08.
                if (vl in ("нет","no") or vl.startswith("без")) and srv is None: srv=0
        if rv is None or (rv==1 and srv and srv>1): rv = srv if srv is not None else rv
        # сдвоенные карточки «8/10» -> кандидат на каждый вариант; одна цена на странице =
        # за МЛАДШИЙ (базовый/дешёвый) вариант, на старшие цену не вешаем (она была бы занижена)
        pairs=sorted(bar_flow_pairs(raw_bar, raw_flow, (fkey or "")+" "+str(raw_flow or "")),
                     key=lambda bf:(bf[0] is None, bf[0] or 0))
        for i,(bar,fl) in enumerate(pairs):
            if kw is None and fl is None: continue
            cp=price.get(u) if (len(pairs)==1 or i==0) else None
            cands[b].append(dict(brand=b, sn=sn, kw=kw, bar=bar or bar_from_text(text), fl=fl, oil=oil,
                                 ff=ff, vsd=vsd, rv=rv, name=nm or slug(u), url=u, site=dm(u),
                                 price=cp, status=status.get(u,""), ip=ip_class(text) or ip_from_specs(d),
                                 cool=(cool_class(text, cool_raw)
                                       or ("water" if b in ("dalgakiran","et")
                                           and re.search(r"\d\s?w\b", nm or "", re.I) else None)),
                                 eng=ceng,
                                 we=we, dr=dr, sku=skus.get(u) or sku2, mnt=mnt,
                                 dim=dim or (dim_value(f"{lwh['l']}x{lwh['w']}x{lwh['h']}", "мм")
                                             if len(lwh)==3 else None)))
    unglue_code(cands)          # тот же слитный код бывает и у конкурентов
    learn_sku_letters(cands)    # буквы, которыми конкурент сам разводит свои SKU
    learn_family_weights(cands) # веса, раздаваемые продавцом по всему семейству
    mark_weak_drive(cands, load_ours_all())
    return cands


def mark_weak_drive(cands, cross=None):
    """Пометить привод, о котором источники СПОРЯТ, как ненадёжный (dr_weak).

    Привод конкуренты берут из шаблона рубрики, а не из паспорта: pnevmoteh пишет
    «ременной» всем 128 карточкам Atlas GA, rutector тем же GA — «прямой» всем 10,
    хотя GA заводом сделан с прямым приводом. По всем моделям, размеченным двумя
    площадками, расхождение в 934 случаях из 5 437 (17%) — на таком признаке резать
    в одиночку нельзя: замер 13.08 показал 214 наших товаров, теряющих ЕДИНСТВЕННЫЙ
    матч из-за привода, и все конфликты приходят из спек-пропа, ни одного из имени.

    Наш каталог тоже спорит сам с собой: «ATLAS COPCO GA 11 10P FM» размечена ременной,
    а «Atlas Copco GA11 10P/400В 3ф 50 Гц/СЕ/FM» — прямой, хотя это один товар. Поэтому
    источником спора считаем и нашу выгрузку: cross отдаёт наши карточки той же модели.

    Метку в ИМЕНИ («ВК-18.5Р» = ременная) это не трогает: она заводская, стоит в
    артикуле и остаётся твёрдым признаком — dr_name читается в pick_cands отдельно."""
    for brand, lst in cands.items():
        by = defaultdict(list)
        for c in lst:
            by[(c["sn"], c.get("kw"), c.get("bar"))].append(c)
        for o in (cross or {}).get(brand, []):
            if o.get("dr"): by[(o["sn"], o.get("kw"), o.get("bar"))].append(
                dict(dr=o["dr"], site="ours", name=o.get("name"), _ours=True))
        for grp in by.values():
            drs = {c["dr"] for c in grp if c.get("dr")}
            if len(drs) > 1 and len({c["site"] for c in grp if c.get("dr")}) > 1:
                for c in grp:
                    # Привод, вынесенный в ИМЯ карточки («ЗИФ-СВЭ-1,0/1,0 ШМ ременная»),
                    # спорным не считаем: это заводская метка, а не поле рубрики. Она
                    # разводит доказанную ложную пару — наша ЗИФ ШМ прямого привода
                    # (155 кг) против их ременной (290 кг).
                    if not dr_name(c.get("name")):
                        c["dr_weak"] = True

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
        # Исполнение сравнивается ПЕРВЫМ: кандидат с тем же SKU у конкурента есть в
        # 6 ложных матчах из 8 (проверка 11.08), и метка разводит их до пропов.
        m=pick_cands(o, by_sn.get(o["sn"], []), brand)
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
