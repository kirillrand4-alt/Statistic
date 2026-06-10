"""ВСЕ бренды: компрессоры конкурентов, у которых характеристики не сняты или неполные
(нет кВт И бар И производительности). Логика 1-в-1 как atlas_need_specs (он же — эталон),
вселенная и правила отсева общие. Atlas включён (его остаток). Итог -> txt на прогон."""
import csv, sys
csv.field_size_limit(sys.maxsize)
from collections import Counter
from matcher import brand_of
from spec_match import is_compressor
from atlas_need_specs import (load_universe, is_product_url, slug, dm, COMPETITORS,
                              has_kw, has_bar, has_flow)

OUT = "/home/user/Statistic/all_brands_need_specs.txt"

def build():
    names, specs, rescanned = load_universe()
    need=[]; nodata=Counter(); complete=Counter()
    by_brand=Counter(); by_site=Counter()
    for u,nm in names.items():
        if dm(u) not in COMPETITORS: continue
        if not is_product_url(u): continue
        b=brand_of(u, nm)
        if not b: continue                        # бренд неизвестен — не матчим, не сканим
        text=(nm or "")+" "+slug(u)
        if not any(ch.isdigit() for ch in text): continue
        if not is_compressor(text): continue
        sd=specs.get(u, {})
        if has_kw(sd) and has_bar(sd) and has_flow(sd):
            complete[b]+=1; continue
        if u in rescanned:                        # новый парсер сканировал — сайт не публикует
            nodata[b]+=1; continue
        need.append((b,u)); by_brand[b]+=1; by_site[dm(u)]+=1

    urls=sorted({u for _,u in need})
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(urls)+"\n")
    tot_c=sum(complete.values()); tot_n=sum(nodata.values())
    print(f"компрессоры конкурентов с брендом: всего {tot_c+tot_n+len(urls)} | "
          f"полные {tot_c} | сайт не публикует {tot_n} | НАДО ЧЕКНУТЬ {len(urls)}")
    print(f"\nпо сайтам: " + ", ".join(f"{s} {c}" for s,c in by_site.most_common()))
    print(f"\nпо брендам (надо чекнуть / полных уже есть):")
    for b,c in by_brand.most_common():
        print(f"  {b:<14} {c:>5}  (полных {complete.get(b,0)})")
    print(f"\n-> {OUT}")

if __name__=="__main__":
    build()
