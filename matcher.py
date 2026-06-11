"""Поссылочный матчер компрессоров. v1 — итеративно дополняется правилами."""
import re
from urllib.parse import urlparse, unquote

# --- слова-типы и единицы: ШУМ (выкидываем) ---
STOP = {
    "vintovoy","vintovye","vintovoj","porshnevoy","porshnevye","porshnevoj",
    "kompressor","kompressory","kompressori","kompressora","elektricheskie",
    "elektricheskij","elektricheskiy","katalog","produkcii","produkciya",
    "stanciya","stancii","kompr","shop","oborudovanie","catalog","product",
    "products","bar","na","s","so","dlya","i",
}
# --- единицы, которые шум сами по себе (но число рядом — сигнал) ---
UNIT_WORDS = {"l","л","kvt","kw","at","atm"}

# --- бренды (синонимы -> канон). Дополняется по мере охвата каталога ---
BRAND_ALIASES = {
    "hansmann":"hansmann","remeza":"remeza","fiac":"fiac","abac":"abac",
    "fini":"fini","aso":"aso","zif":"zif","comaro":"comaro","atom":"atom",
    "enger":"enger","airrus":"airrus","elitech":"elitech","ekomak":"ekomak",
    "ceccato":"ceccato","dali":"dali","kraft":"kraft","fubag":"fubag",
    # добавлено: крупные бренды каталога
    "dalgakiran":"dalgakiran","atlas":"atlas","berg":"berg","almig":"almig",
    "ariacom":"ariacom","kraftmann":"kraftmann","magnus":"magnus","atmos":"atmos",
    "airpol":"airpol","lupamat":"lupamat","comprag":"comprag","ozen":"ozen",
    "ironmac":"ironmac","airman":"airman","buster":"buster","ultratech":"ultratech",
    "comprecit":"comprecit","kaeser":"kaeser","chinook":"chinook","cross":"cross",
    "dgk":"dalgakiran","et":"et",
    # батч №3 — остальные бренды каталога
    "renner":"renner","boge":"boge","kraftmachine":"kraftmachine","zega":"zega",
    "master":"master","zuv":"zuv","coaire":"coaire","das":"das","harrison":"harrison",
    "global":"global","zammer":"zammer","sullair":"sullair","mark":"mark","chicago":"chicago",
    "ingro":"ingro","baysar":"baysar","wis":"wis","tamsan":"tamsan","habe":"habe",
    "crossair":"cross","spitzenreiter":"spitzenreiter","paramina":"paramina","sigma":"sigma",
    "aztec":"aztec","chkz":"chkz","hori":"hori","mig":"mig","mmz":"mmz","spr":"spr",
    "ir":"ir","ingersoll":"ir","rand":"ir",
    "ac":"atlas",   # «AC» = Atlas Copco (у нас: «AC GA22 VSD+», «AC ZR 110»; v-p-k: aq-…-ac-ff)
    # кириллические имена производителей (Битрикс-выгрузка пишет кириллицей)
    "зиф":"zif","ркз":"mig","бежецк":"aso","чкз":"chkz","минский":"mmz","ремеза":"remeza",
    # конкурентские бренды, которых нет у prokompressor (на рассмотрение)
    "xeleron":"xeleron","gmp":"gmp","kaishan":"kaishan","denair":"denair","vortex":"vortex",
    "brestor":"brestor","baldor":"baldor","alup":"alup",
}

# --- IP-рейтинг по умолчанию (шум). Остальные IP — сигнал ---
IP_DEFAULT = {"ip23"}

def domain(u):
    try: return urlparse(str(u)).netloc.lower().replace("www.","")
    except: return "?"

def path_tokens(u):
    p = unquote(urlparse(str(u)).path).strip("/").lower()   # %D0%B2 -> в (раскодируем кириллицу)
    return [t for t in re.split(r"[-_/.\s]+", p) if t]

def last_segment_tokens(u):
    p = unquote(urlparse(str(u)).path).strip("/").lower()
    seg = p.split("/")[-1] if p else ""
    return [t for t in re.split(r"[-_/.\s]+", seg) if t]

def find_brand(u):
    """Бренд ищем по всему пути (у части сайтов он отдельным сегментом)."""
    toks = path_tokens(u)
    for i, t in enumerate(toks):
        b = BRAND_ALIASES.get(t)
        if not b: continue
        if t == "ac":      # «ac» = Atlas только если СЛЕДУЮЩИЙ токен — серия Atlas («ac ga22»);
            nxt = toks[i+1] if i+1 < len(toks) else ""   # иначе это air-cooled (slt…ac…ip23 и пр.)
            if not _is_atlas_series(nxt): continue
        return b
    return None

_ATLAS_SER = {"xahs","xrhs","xrvs","xrys","xrxs","xats","xavs","xas","ga","gx",
              "zr","zt","ze","za","aq","gv","le","lf","lt","sf"}
def _is_atlas_series(tok):
    m = re.match(r"([a-z]+)\d*$", str(tok))
    return bool(m) and m.group(1) in _ATLAS_SER

# бренд по НАЗВАНИЮ (фолбэк: часть сайтов, напр. v-p-k, не пишут бренд в URL).
# Исключаем короткие двусмысленные токены: «ac» в названии = воздушное охлаждение
# («Olymtech ... (AC)»), а не Atlas Copco. В URL-слаге «ac» оставляем (find_brand).
_UNSAFE_IN_TEXT = {"ac"}
_NAME_BRANDS = sorted((set(BRAND_ALIASES) - _UNSAFE_IN_TEXT) | {"atlas copco","ingersoll rand","cross air"},
                      key=len, reverse=True)
def brand_from_text(text):
    """Распознать бренд в произвольном тексте (название товара). Длинные имена раньше."""
    t = " " + re.sub(r"[^a-zа-я0-9 ]", " ", str(text).lower()) + " "
    for b in _NAME_BRANDS:
        if " " + b + " " in t:
            return BRAND_ALIASES.get(b, BRAND_ALIASES.get(b.split()[0], b.split()[0]))
    return None

def brand_of(u, name=""):
    """Бренд: сперва из URL, иначе — из названия товара."""
    return find_brand(u) or (brand_from_text(name) if name else None)

def _merge_ip(tokens):
    """ip + 54  ->  ip54 ; ip-54 уже разбит на ip,54."""
    out=[]; i=0
    while i < len(tokens):
        if tokens[i]=="ip" and i+1<len(tokens) and tokens[i+1].isdigit():
            out.append("ip"+tokens[i+1]); i+=2
        else:
            out.append(tokens[i]); i+=1
    return out

def _split_num_letter(tokens):
    """500dr -> 500, dr  (число + короткий хвост букв). rs15a НЕ трогаем (нач. с букв)."""
    out=[]
    for t in tokens:
        m=re.fullmatch(r"(\d+)([a-z]{1,3})", t)
        out += [m.group(1), m.group(2)] if m else [t]
    return out

def model_tokens(u):
    brand = find_brand(u)
    toks = last_segment_tokens(u)
    toks = [t for t in toks if t not in STOP]   # стоп/предлоги — ДО разбиения (чтобы 10s сохранил s-вариант)
    toks = _split_num_letter(toks)
    toks = _merge_ip(toks)
    model=[]
    for t in toks:
        if t in UNIT_WORDS: continue
        if t in BRAND_ALIASES: continue          # бренд убираем из ядра
        if re.fullmatch(r"ip\d+", t) and t in IP_DEFAULT: continue
        model.append(t)
    # ВК (винтовой компрессор) транслитерируется и как vk (мы), и как bk (конкуренты): В→V/B
    model = [("vk"+t[2:]) if (t=="bk" or re.fullmatch(r"bk\d.*", t)) else t for t in model]
    return brand, model

def _classify(model):
    """Разложить токены ядра на: СКЕЛЕТ (слова модели; в signature сортируются — порядок слов
    между сайтами нестабилен: peredvizhnoy в начале/конце, «5 Plus»/«Plus 5»), однозначные
    числа (порядок важен — части дробей), одиночные буквы, многозначные числа."""
    skeleton=[]; onedig=[]; singles=[]; multidig=[]
    for t in model:
        if re.fullmatch(r"\d", t):            # одна цифра — часть дроби (2,5)
            onedig.append(t)
        elif re.fullmatch(r"\d+", t):         # многозначное число — ресивер/объём
            multidig.append(t)
        elif re.fullmatch(r"[a-zа-я]", t):    # одна буква — вариант (D, U, Ex...)
            singles.append(t)
        else:                                  # слово модели (sl, vs, pm, dr, nt3, rs15e, ip55)
            skeleton.append(t)
    return skeleton, onedig, singles, multidig

def signature(u):
    """Умный отпечаток. Безопасные перестановки (давление в конце/середине, ресивер↔
    давление, D VS↔VS D) дают ОДИН ключ; структурные различия (Inversys «7 Plus»≠«Plus»,
    ZIF 2,5≠5,2) — РАЗНЫЕ ключи. Поля: бренд :: скелет :: одноцифры(порядок) ::
    буквы(сорт) :: многоцифры(ресивер=макс плавает, мощность/давление в порядке)."""
    brand, model = model_tokens(u)
    sk, one, sg, mu = _classify(model)
    if mu:
        vals=sorted((int(x) for x in mu), reverse=True)
        if len(mu)==1 or vals[0] >= 2*vals[1]:   # есть явный ресивер (макс ≥2× след.) — он плавает
            mx=max(mu, key=int); rest=list(mu); rest.remove(mx)
            mu_field="|".join(rest)+">"+mx
        else:                                     # близкие величины (мощн./давл.) — порядок значим
            mu_field="|".join(mu)
    else:
        mu_field=""
    return (f"{brand or '?'}::" + "|".join(sorted(sk)) + "::" + "|".join(one)
            + "::" + "|".join(sorted(sg)) + "::" + mu_field)

if __name__ == "__main__":
    import sys
    for u in sys.argv[1:]:
        print(signature(u), "<-", u)
