"""Отдельные категории конкурентов на сканирование (не компрессоры — в основной список
не входят): ОСУШИТЕЛИ, РЕСИВЕРЫ/воздухосборники, АЗОТНЫЕ станции/генераторы.
Критерий «надо добавить» = новый парсер ещё не проходил по URL (нет в rescanned).
Бренд НЕ обязателен («всё что может пригодиться даже в теории»)."""
import csv, sys, re
csv.field_size_limit(sys.maxsize)
from collections import Counter
from spec_match import is_compressor
from atlas_need_specs import load_universe, is_product_url, slug, dm, COMPETITORS, dedup_urls

DIR="/home/user/Statistic/"
CATS = [  # (имя файла, регэксп по имени+слагу, анти-регэксп)
    ("osushiteli",  re.compile(r'осушител|osushitel|\bdryer', re.I),
                    re.compile(r'фильтр|filtr|картридж|сервис|servis|ремкомплект', re.I)),
    ("resivery",    re.compile(r'\bресивер|\bresiver|воздухосборник|vozduhosbornik|возд\w*сборник', re.I),
                    re.compile(r'фильтр|filtr|клапан|klapan|манометр|прокладк|сервис', re.I)),
    ("azot",        re.compile(r'азот\w*|azot|nitrogen', re.I),
                    re.compile(r'фильтр|filtr|картридж|сервис|мембран\w+\s+для', re.I)),
]

# Леаф-страница = СЕРИЯ/ДИАПАЗОН/категория-фильтр, а не один товар (особенно aerocompressors:
# осушители сгруппированы по производительности «osushiteli_na_10_m3_min», по версиям
# «..._versiya_...», по линейкам «seriya/serii», диапазонами «1500-15600 l_min»). Парсить их
# бессмысленно — паспорта одного SKU не дадут. Смотрим ТОЛЬКО последний сегмент (родительские
# папки вроде «..._serii_d/» не считаем — там в леафе реальный товар).
_SERIES_LEAF = re.compile(
    r'(?:^|[_-])osushiteli_na_\d'                              # фильтр-категория по производительности
    r'|(?:^|[_-])(?:seriya|versiya|serii)(?:[_-]|$)'           # серия/версия/линейка
    r'|\d+\s*[-_–]+\s*\d+\s*_?(?:l[_ ]?min|lmin|m3[_ ]?min|mmin|m_min|kvt|kw)'  # диапазон произв./мощн.
    r'|\d{7,}\s*_?l[_ ]?min',                                  # склеенный диапазон (6000-18000 -> 600018000_lmin)
    re.I)
def is_series_or_range_leaf(u):
    return bool(_SERIES_LEAF.search(slug(u)))

def build():
    names, specs, rescanned = load_universe()
    out={c[0]:[] for c in CATS}; by=Counter()
    for u,nm in names.items():
        if dm(u) not in COMPETITORS: continue
        if not is_product_url(u): continue
        text=(nm or "")+" "+slug(u)
        if not any(ch.isdigit() for ch in text): continue
        if is_compressor(text): continue          # компрессоры/станции — в основном списке
        if is_series_or_range_leaf(u): continue    # серия/диапазон/категория-фильтр, не один товар
        if u in rescanned: continue               # уже пройден новым парсером
        for cat,rx,anti in CATS:
            if rx.search(text) and not anti.search(text):
                out[cat].append(u); by[(cat,dm(u))]+=1
                break
    for cat,_,_ in CATS:
        path=DIR+cat+"_need_scan.txt"
        with open(path,"w",encoding="utf-8") as fh:
            fh.write("\n".join(dedup_urls(out[cat]))+"\n")
        sites=", ".join(f"{s} {c}" for (cc,s),c in by.most_common() if cc==cat)
        print(f"{cat:<12} {len(dedup_urls(out[cat])):>5}  ({sites})")
    print("-> *_need_scan.txt")

if __name__=="__main__":
    build()
