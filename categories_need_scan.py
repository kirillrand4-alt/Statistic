"""Отдельные категории конкурентов на сканирование (не компрессоры — в основной список
не входят): ОСУШИТЕЛИ, РЕСИВЕРЫ/воздухосборники, АЗОТНЫЕ станции/генераторы.
Критерий «надо добавить» = новый парсер ещё не проходил по URL (нет в rescanned).
Бренд НЕ обязателен («всё что может пригодиться даже в теории»)."""
import csv, sys, re
csv.field_size_limit(sys.maxsize)
from collections import Counter
from spec_match import is_compressor
from atlas_need_specs import load_universe, is_product_url, slug, dm, COMPETITORS

DIR="/home/user/Statistic/"
CATS = [  # (имя файла, регэксп по имени+слагу, анти-регэксп)
    ("osushiteli",  re.compile(r'осушител|osushitel|\bdryer', re.I),
                    re.compile(r'фильтр|filtr|картридж|сервис|servis|ремкомплект', re.I)),
    ("resivery",    re.compile(r'\bресивер|\bresiver|воздухосборник|vozduhosbornik|возд\w*сборник', re.I),
                    re.compile(r'фильтр|filtr|клапан|klapan|манометр|прокладк|сервис', re.I)),
    ("azot",        re.compile(r'азот\w*|azot|nitrogen', re.I),
                    re.compile(r'фильтр|filtr|картридж|сервис|мембран\w+\s+для', re.I)),
]

def build():
    names, specs, rescanned = load_universe()
    out={c[0]:[] for c in CATS}; by=Counter()
    for u,nm in names.items():
        if dm(u) not in COMPETITORS: continue
        if not is_product_url(u): continue
        text=(nm or "")+" "+slug(u)
        if not any(ch.isdigit() for ch in text): continue
        if is_compressor(text): continue          # компрессоры/станции — в основном списке
        if u in rescanned: continue               # уже пройден новым парсером
        for cat,rx,anti in CATS:
            if rx.search(text) and not anti.search(text):
                out[cat].append(u); by[(cat,dm(u))]+=1
                break
    for cat,_,_ in CATS:
        path=DIR+cat+"_need_scan.txt"
        with open(path,"w",encoding="utf-8") as fh:
            fh.write("\n".join(sorted(set(out[cat])))+"\n")
        sites=", ".join(f"{s} {c}" for (cc,s),c in by.most_common() if cc==cat)
        print(f"{cat:<12} {len(set(out[cat])):>5}  ({sites})")
    print("-> *_need_scan.txt")

if __name__=="__main__":
    build()
