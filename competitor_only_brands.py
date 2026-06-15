"""Бренды конкурентов, которых НЕТ в нашем каталоге (обратный GAP уровня бренда).
comp_brands (компрессоры load_comp_all + категории load_comp_cat) минус наши бренды.

Два предохранителя против ложных срабатываний (оба найдены на реальных данных):
 1) ТОКЕН-ГАРД: бренд считаем «нашим», если его токен встречается в сыром каталоге,
    даже если brand_from_text его не распознал. Ловит KonDR (наши ресиверы «KonDR РВ»)
    и ALUP (у нас только запчасти «Фильтр ALUP») — без него попадали в список ошибочно.
 2) ДОМЕН-ГАРД: если карточки бренда сидят в основном на prokompressor.ru — это не бренд,
    а артефакт разбора (buster <- «бустер-компрессор» Ozen на нашем же сайте). Выкидываем."""
import csv, sys
csv.field_size_limit(sys.maxsize)
from collections import Counter, defaultdict
from matcher import brand_from_text, BRAND_ALIASES, brand_of
from brand_spec_review import load_comp_all
from category_spec_review import load_comp_cat
from atlas_need_specs import load_universe, dm, is_product_url
from spec_match import is_compressor
from scrape_files import U

SPECS_CSV=U+"specs2/specs_compact.csv"
OUT="/home/user/Statistic/Бренды_конкурентов_нет_у_нас.csv"
COMP=["compressortyt.ru","aerocompressors.ru","pnevmoteh.ru","pnevmo-sklad.ru","v-p-k.ru","rutector.ru"]

def brand_any(t):
    if not t: return None
    b=brand_from_text(t)
    if b: return b
    w=t.lower().split(); return BRAND_ALIASES.get(w[0],None) if w else None

def klass(s):
    s=s.lower()
    if "осушит" in s: return "осушители"
    if "азот" in s or "генератор" in s: return "азот"
    if "ресивер" in s or "воздухосборник" in s: return "ресиверы"
    if "компрессор" in s: return "компрессоры"
    return "иное"

def build():
    # наши бренды (по распознаванию) — первый проход по каталогу
    our=set();
    for r in csv.DictReader(open(SPECS_CSV,encoding="utf-8-sig",errors="replace"),delimiter=";"):
        b=brand_any((r.get("IP_PROP22553") or "").strip()) or brand_from_text(r.get("IE_NAME",""))
        if b: our.add(b)
    # бренды конкурентов (компрессоры + категории)
    comp=set(load_comp_all())
    for ck,bm in load_comp_cat().items(): comp|=set(bm)
    cand=sorted(comp-our)

    # ТОКЕН-ГАРД: второй проход — какие токены кандидатов реально есть в каталоге
    intoken={c:False for c in cand}
    for r in csv.DictReader(open(SPECS_CSV,encoding="utf-8-sig",errors="replace"),delimiter=";"):
        blob=((r.get("IP_PROP22553") or "")+" "+(r.get("IE_NAME") or "")).lower()
        for c in cand:
            if not intoken[c] and c in blob: intoken[c]=True
    cand=[c for c in cand if not intoken[c]]   # убрали «наши» по токену (KonDR/ALUP)

    # карточки конкурентов + ДОМЕН-ГАРД (наш домен считаем тем же brand_of — он знает
    # алиас «бустер»->buster, поэтому артефакт ловится, а не проскакивает мимо латиницы)
    names,_,_=load_universe()
    candset=set(cand)
    cards=defaultdict(set); site=defaultdict(Counter); kw=defaultdict(Counter); ex=defaultdict(str); own=Counter()
    for u,nm in names.items():
        if not is_product_url(u): continue
        d=dm(u)
        if d=="prokompressor.ru":
            ob=brand_of(u,nm)
            if ob in candset: own[ob]+=1
            continue
        if d not in COMP: continue
        bb=brand_of(u,nm)
        if bb not in candset or u in cards[bb]: continue
        cards[bb].add(u); site[bb][d]+=1; kw[bb][klass((nm or "")+" "+u)]+=1
        if "компрессор" not in (ex[bb] or "").lower() and nm and "компрессор" in nm.lower(): ex[bb]=nm[:50]
        elif not ex[bb] and nm: ex[bb]=nm[:50]

    rows=[]; dropped=[]
    for b in sorted(cand, key=lambda x:-len(cards[x])):
        n=len(cards[b])
        if n==0 or own[b]>n:            # домен-гард: на нашем сайте карточек больше, чем у конкурентов
            dropped.append((b, f"артефакт/наш домен (конкур {n}, prokompressor {own[b]})")); continue
        tp=", ".join(t for t,_ in kw[b].most_common(2) if t!="иное") or "иное"
        ss=", ".join(f"{s}:{c}" for s,c in site[b].most_common(3))
        rows.append([b.upper(), tp, n, ss, ex[b]])
    with open(OUT,"w",encoding="utf-8-sig",newline="") as fh:
        w=csv.writer(fh,delimiter=";"); w.writerow(["бренд","тип товара","карточек у конкур","топ-сайты","пример"]); w.writerows(rows)
    for r in rows: print(f"{r[0]:<11}{r[1]:<24}{r[2]:>5}  {r[4][:42]}")
    if dropped:
        print("отброшено (предохранители):")
        for b,why in dropped: print(f"  {b} — {why}")
    print(f"-> {OUT} | брендов: {len(rows)}")

if __name__=="__main__":
    build()
