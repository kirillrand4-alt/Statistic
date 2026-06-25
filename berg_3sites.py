"""Три сайта Berg друг против друга: berg-compressor.com, berg-kompressor.ru, prokompressor.ru.
Ключ товара = модель Berg, извлечённая из СЛАГА URL одинаково для всех (ser_of по де-слагу),
чтобы разные конвенции именования сошлись. Матрица присутствия + статус-гэпы.
Оговорка: ключ — по коду модели из слага; запчасти (фильтры B###, ремни XPA###) дают шум и
односимвольные серии (b/c/m) могут коллизировать — это грубая прикидка гэпов, не паспортный матч."""
import csv, sys, os, re
csv.field_size_limit(sys.maxsize)
from urllib.parse import urlparse
from matcher import brand_from_text, find_brand
from brand_spec_review import ser_of

F="/root/.claude/uploads/62a19005-a7bf-569b-926e-b59b4a62600d/06f7293a-________________________.txt"
CAT="/root/.claude/uploads/62a19005-a7bf-569b-926e-b59b4a62600d/426094e6-prokompressor.ru.csv"
OUT="/home/user/Statistic/Berg_3sites_compare.csv"

def keyfromurl(u):
    segs=[s for s in urlparse(u).path.strip("/").split("/") if s]
    if not segs or segs[0]!="catalog": return None
    slug=segs[-1]
    if not re.search(r"\d", slug): return None          # товар = есть число в коде
    return ser_of("berg "+slug.replace("-"," ").replace("_"," "), "berg")

def build():
    com={}; ru={}
    for l in open(F, encoding="utf-8", errors="replace"):
        u=l.strip()
        if not u.startswith("http"): continue
        d=urlparse(u).netloc.replace("www.","")
        k=keyfromurl(u)
        if k: (com if ".com" in d else ru).setdefault(k, u)
    proko={}; proko_bynum={}
    for r in csv.DictReader(open(CAT, encoding="utf-8-sig"), delimiter=";"):
        nm=(r.get("Название") or "").strip(); u=(r.get("URL") or "").strip()
        if (brand_from_text(nm) or find_brand(u))!="berg": continue
        k=keyfromurl(u)
        if k:
            proko.setdefault(k, u)
            proko_bynum.setdefault(k[1], []).append(f"{str(k[0]).upper()}:{nm[:40]}")

    keys=set(com)|set(ru)|set(proko)
    STAT={frozenset(["com","ru","proko"]):"везде (все 3)",
          frozenset(["com","ru"]):"berg.com+ru — у НАС нет",
          frozenset(["com","proko"]):"berg.com+мы — на .ru нет",
          frozenset(["ru","proko"]):"berg.ru+мы — на .com нет",
          frozenset(["com"]):"только berg.com",
          frozenset(["ru"]):"только berg.ru",
          frozenset(["proko"]):"только у нас (proko)"}
    rows=[]; from collections import Counter; cnt=Counter()
    for k in keys:
        pres=set()
        if k in com: pres.add("com")
        if k in ru: pres.add("ru")
        if k in proko: pres.add("proko")
        st=STAT[frozenset(pres)]; cnt[st]+=1
        model=f"{str(k[0]).upper()} {k[1]:g}"
        # если у нас «нет» — показываем, что есть по ЭТОМУ номеру (ловит ложные гэпы: D028=RSP-D028 и т.п.)
        nearby="; ".join(dict.fromkeys(proko_bynum.get(k[1],[]))) if "proko" not in pres else ""
        rows.append([model, "✓" if "com" in pres else "", "✓" if "ru" in pres else "",
                     "✓" if "proko" in pres else "", st, nearby,
                     com.get(k,""), ru.get(k,""), proko.get(k,"")])
    order=list(STAT.values())
    rows.sort(key=lambda r:(order.index(r[4]), r[0]))
    with open(OUT,"w",encoding="utf-8-sig",newline="") as fh:
        w=csv.writer(fh,delimiter=";")
        w.writerow(["модель","berg.com","berg.ru","prokompressor","статус","у нас по этому №","ссылка berg.com","ссылка berg.ru","ссылка proko"])
        w.writerows(rows)
    print(f"ключей-моделей: .com={len(com)} | .ru={len(ru)} | proko={len(proko)} | объединение={len(keys)}")
    for st in order: print(f"  {st:<28} {cnt[st]}")
    print(f"-> {OUT}")

if __name__=="__main__":
    build()
