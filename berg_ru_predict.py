"""Предположить, как назывались бы на berg-kompressor.ru товары, которых там НЕТ,
но есть на berg-compressor.com или prokompressor (Berg + дочерний бренд Atom).
Ключ — «полный код» из слага (схлопнут: a-4e=a4e, rsp-d=d), чтобы стыковать конвенции.
Генерим .ru-слаг по шаблону семейства (см. _RU)."""
import csv, sys, re
csv.field_size_limit(sys.maxsize)
from urllib.parse import urlparse
from matcher import brand_from_text, find_brand

F="/root/.claude/uploads/62a19005-a7bf-569b-926e-b59b4a62600d/06f7293a-________________________.txt"
CAT="/root/.claude/uploads/62a19005-a7bf-569b-926e-b59b4a62600d/426094e6-prokompressor.ru.csv"
OUT="/home/user/Statistic/Berg_ru_predicted_missing.csv"

TYPE={"vintovoy","vintovye","kompressor","kompressory","kompressornye","stantsii","modulnye",
 "osushitel","osushiteli","adsorbtsionnyy","adsorbtsionnye","refrizheratornyy","refrizheratornye",
 "vozdukha","vozdushnyy","vozdushnye","vozdukhopodgotovka","separator","tsentrobezhnyy","tsiklonnyy",
 "filtr","filtry","magistralnyy","magistralnye","resiver","resivery","berg","na","s","i","dlya",
 "pryamym","remennym","privodom","regeneratsiya","goryachaya","original","komplekt"}

def model_tokens(slug):
    toks=[t for t in slug.split("-") if t]
    while toks and toks[0] in TYPE: toks.pop(0)
    # схлопываем atom "a-4e"->"a4e"; rsp-d -> d
    s="-".join(toks)
    s=re.sub(r"\batom-a-(\d)", r"atom-a\1", s)     # a-4e -> a4e
    s=s.replace("rsp-d","d")
    return [t for t in s.split("-") if t]

def core(slug):
    toks=[t for t in model_tokens(slug) if t!="bar"]
    return re.sub(r"[^a-z0-9]","","".join(toks))

# шаблон .ru: (префикс, отбросить-ли первый токен серии из модели)
_RU=[("atom",  "kompressor-vintovoy-atom-",                 True),
     ("vk",    "vintovoy-kompressor-berg-",                 False),
     ("oh",    "adsorbtsionnyy-osushitel-berg-",            False),
     ("os",    "adsorbtsionnyy-osushitel-berg-",            False),
     ("ov",    "osushitel-refrizheratornyy-berg-",          False),
     ("ob",    "refrizheratornyy-osushitel-vozdukha-berg-", False),
     ("d",     "separator-tsentrobezhnyy-tsiklonnyy-berg-", False),
     ("rsp",   "filtr-magistralnyy-berg-",                  False),
     ("rv",    "vozdushnyy-resiver-berg-",                  False)]
def family(mt):
    t0=mt[0] if mt else ""
    for pref,_,_ in [("atom","",0)]: pass
    for key,_,_ in _RU:
        if t0==key or t0.startswith(key) or (key=="d" and re.match(r"d\d",t0)): return key
    return None

def ru_slug(mt):
    fam=family(mt)
    for key,pref,strip in _RU:
        if key==fam:
            m=mt[1:] if strip else mt
            return pref+"-".join(m)
    return "berg-"+"-".join(mt)

def label(mt, fam):
    code=" ".join(mt[1:] if fam=="atom" else mt).upper().replace(" BAR"," бар")
    typ={"atom":"Компрессор Atom A","vk":"Винтовой компрессор Berg","oh":"Адс. осушитель Berg",
         "os":"Адс. осушитель Berg","ov":"Рефр. осушитель Berg","ob":"Рефр. осушитель Berg",
         "d":"Сепаратор Berg","rsp":"Фильтр Berg","rv":"Ресивер Berg"}.get(fam,"Berg")
    if fam=="atom": code=code.lstrip("A ")        # atom A4E -> typ уже "...Atom A"
    return f"{typ}{code if fam=='atom' else ' '+code}"

def load():
    com={}; ru={}
    for l in open(F,encoding="utf-8",errors="replace"):
        u=l.strip()
        if not u.startswith("http"): continue
        d=urlparse(u).netloc.replace("www.",""); segs=[s for s in urlparse(u).path.strip("/").split("/") if s]
        if len(segs)<2 or segs[0]!="catalog": continue
        slug=segs[-1]
        if not re.search(r"\d",slug): continue
        (com if ".com" in d else ru).setdefault(core(slug), (slug,u))
    proko={}
    for r in csv.DictReader(open(CAT,encoding="utf-8-sig"),delimiter=";"):
        nm=(r.get("Название") or "").strip(); u=(r.get("URL") or "").strip()
        b=brand_from_text(nm) or find_brand(u)
        if b not in ("berg","atom"): continue                 # Atom = дочерний бренд Berg
        segs=[s for s in urlparse(u).path.strip("/").split("/") if s]
        if not segs: continue
        slug=segs[-1]
        if not re.search(r"\d",slug): continue
        proko.setdefault(core(slug), (slug,u))
    return com,ru,proko

def build():
    com,ru,proko=load()
    rk=set(ru)
    rows=[]
    src={}
    for c,(slug,u) in com.items():
        if c not in rk: src.setdefault(c,{"slug":slug,"where":set(),"url":u}); src[c]["where"].add("berg.com")
    for c,(slug,u) in proko.items():
        if c not in rk:
            src.setdefault(c,{"slug":slug,"where":set(),"url":u}); src[c]["where"].add("proko")
    from collections import Counter
    famc=Counter()
    for c,info in src.items():
        mt=model_tokens(info["slug"])
        fam=family(mt); famc[fam]+=1
        rows.append([fam or "?", label(mt,fam), "https://berg-kompressor.ru/catalog/"+ru_slug(mt)+"/",
                     "+".join(sorted(info["where"])), info["url"]])
    famorder=["atom","vk","oh","os","ov","ob","d","rsp","rv",None]
    rows.sort(key=lambda r:(famorder.index(r[0]) if r[0] in famorder else 99, r[1]))
    with open(OUT,"w",encoding="utf-8-sig",newline="") as fh:
        w=csv.writer(fh,delimiter=";")
        w.writerow(["семейство","модель","предполагаемый URL berg.ru","где есть","ссылка-источник"])
        w.writerows(rows)
    print(f"кодов: .com={len(com)} .ru={len(ru)} proko={len(proko)} | НЕТ на .ru (из com|proko): {len(rows)}")
    for f in famorder:
        if famc[f]: print(f"  {str(f):<6} {famc[f]}")
    print(f"-> {OUT}")
    # ATOM крупным планом
    atom=[r for r in rows if r[0]=="atom"]
    print(f"\nATOM нет на .ru: {len(atom)} (примеры):")
    for r in atom[:8]: print("  ",r[1],"->",r[2].split('/catalog/')[-1])

if __name__=="__main__":
    build()
