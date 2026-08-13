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
    for t in model_code(text, brand)[0]:
        if re.fullmatch(r"[\d.]+", t) or t in _CYR_PREP:
            continue
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


_EXEC_MNT=(("салазк","салазки"),("подвес","подвес"),("на скатах","скаты"),
           ("без шасси","рама"),("на раме","рама"),("шасси","шасси"))
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
    return tags


def exec_filter(o_name, cands):
    """Направленный фильтр исполнения станции (см. exec_tags). У кандидата к имени
    приклеивается спека «Исполнение»: rutector пишет «На раме / Стационарный» именно
    там, а имя оставляет чистым — наш «ЗИФ ПВ-16/1,0 (на шасси)» иначе не отличить."""
    ot=exec_tags(o_name)
    out=[]
    for c in cands:
        ct=exec_tags((c.get("name") or "")+" "+(c.get("mnt") or ""))
        if any(k in ot and k in ct and ot[k]!=ct[k] for k in ("кожух","монтаж","север","тормоз","сеть")):
            continue
        out.append(c)
    return out


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
    m = (variant_filter(o.get("name", ""), m, brand) if VARIANT_STRICT
         else prefer_exact_variant(o.get("name", ""), m))
    return ff_filter(o.get("ff"), receiver_filter(o.get("rv"), m), o.get("name", ""))

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
        ours[b].append(dict(brand=b, sn=sn, kw=sane_kw(num(r.get("IP_PROP22562"))),
                            bar=bar_value(r.get("IP_PROP22573")) or bar_from_text(name+" "+code),
                            fl=fl, oil=oil_of(r.get("IP_PROP22583")), ff=ff, vsd=vsd, rv=rv,
                            name=nm or name, url=url, price=p,
                            ip=(ip_class(name+" "+code)
                                or next((v for k in ipcols if (v:=ip_class(r.get(k,"")))), None)
                                or ourip.get(code.lower())
                                or next((v for k in ipopen
                                         if (v:=ip_class(r.get(k,""))) and _ip_open(v)), None)),
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
        ff,rv = suffix_flags(nm or slug(u), b, ff, rv)
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
        mnt=next((str(v) for k,v in d.items() if k.strip().lower()=="исполнение"), None)
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
                                 cool=cool_class(text, cool_raw),
                                 we=we, dr=dr, sku=skus.get(u) or sku2, mnt=mnt))
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
