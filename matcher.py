"""Поссылочный матчер компрессоров. v1 — итеративно дополняется правилами."""
import re
from urllib.parse import urlparse

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
}

# --- IP-рейтинг по умолчанию (шум). Остальные IP — сигнал ---
IP_DEFAULT = {"ip23"}

def domain(u):
    try: return urlparse(str(u)).netloc.lower().replace("www.","")
    except: return "?"

def path_tokens(u):
    p = urlparse(str(u)).path.strip("/").lower()
    return [t for t in re.split(r"[-_/.\s]+", p) if t]

def last_segment_tokens(u):
    p = urlparse(str(u)).path.strip("/").lower()
    seg = p.split("/")[-1] if p else ""
    return [t for t in re.split(r"[-_/.\s]+", seg) if t]

def find_brand(u):
    """Бренд ищем по всему пути (у части сайтов он отдельным сегментом)."""
    for t in path_tokens(u):
        if t in BRAND_ALIASES:
            return BRAND_ALIASES[t]
    return None

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
    return brand, model

def _classify(model):
    """Разложить токены ядра на: СКЕЛЕТ (слова модели в порядке, БЕЗ маркеров разрыва —
    маркер ложно разъединял один товар, когда сайты ставят давление в разное место),
    однозначные числа (порядок важен — части дробей), одиночные буквы, многозначные числа."""
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
    return (f"{brand or '?'}::" + "|".join(sk) + "::" + "|".join(one)
            + "::" + "|".join(sorted(sg)) + "::" + mu_field)

if __name__ == "__main__":
    import sys
    for u in sys.argv[1:]:
        print(signature(u), "<-", u)
