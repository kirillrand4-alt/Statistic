"""ЭКСПЕРИМЕНТ: матч от спеков, а не от серии. Берём ВСЕ ссылки (до отсечений is_compressor/
серия), извлекаем производительность+давление+мощность; кластеризуем по ним; потом сужаем
брендом, серией, ресивером. Цель — найти НАШИ товары, у которых есть спек-двойник конкурента
ТОГО ЖЕ бренда, а серийный матчер их НЕ сцепил (значит серия/фильтр промахнулись)."""
import csv, sys, re
csv.field_size_limit(sys.maxsize)
from collections import defaultdict, Counter
from atlas_need_specs import load_universe, is_product_url, slug, dm, COMPETITORS
from matcher import brand_of
from spec_match import (num, sane_kw, bar_value, bar_from_text, flow_value, bar_flow_pairs,
                        is_compressor, match, receiver_filter)
from brand_spec_review import load_ours_all, load_comp_all, ser_of

def spec_of(d):
    kw=None; rb=rf=fk=None
    for k,v in d.items():
        kl=k.lower()
        if kw is None and "мощ" in kl and "шум" not in kl and "звук" not in kl: kw=sane_kw(num(v))
        if rb is None and "давлен" in kl: rb=v
        if rf is None and "произв" in kl: rf=v; fk=kl
    out=[]
    for bar,fl in bar_flow_pairs(rb, rf, (fk or "")+" "+str(rf or "")):
        if kw and bar and fl: out.append((kw,bar,fl))
    return out

def build():
    names, specs, _ = load_universe()
    # 1) RAW-вселенная конкурентов: ВСЕ ссылки со спеками (kw+bar+fl), без отсечений
    raw=[]
    for u,nm in names.items():
        if dm(u) not in COMPETITORS or not is_product_url(u): continue
        d=specs.get(u,{})
        if not d: continue
        bb=brand_of(u,nm); sr=ser_of((nm or "")+" "+slug(u), bb or "")
        for kw,bar,fl in spec_of(d):
            raw.append(dict(kw=kw,bar=bar,fl=fl, brand=bb,
                            iscomp=is_compressor((nm or "")+" "+slug(u)), ser=sr,
                            name=(nm or slug(u)), url=u, site=dm(u)))
    idx=defaultdict(list)
    for c in raw: idx[(round(c["kw"]), round(c["bar"]))].append(c)
    print(f"RAW конкурентских записей со спеками kw+bar+fl: {len(raw)}")

    # 2) серийно-сматченные наши (как в основном отчёте)
    ours_all=load_ours_all(); cands_all=load_comp_all()
    matched_urls={}     # (brand,oururl) -> set сматченных URL конкурентов
    for b in ours_all:
        by=defaultdict(list)
        for c in cands_all.get(b,[]): by[c["sn"]].append(c)
        for o in ours_all[b]:
            m=receiver_filter(o.get("rv"), match(o, by.get(o["sn"], [])))
            matched_urls[(b,o["url"])]=set(c["url"] for c in m)

    # 3) для КАЖДОГО нашего товара ищем спек-двойников ТОГО ЖЕ бренда, которых серийный матч НЕ взял
    TOL_KW=0.06; TOL_BAR=0.05; TOL_FL=0.05
    # ДЕЙСТВУЮЩИЙ промах: спеки совпали + тот же бренд + (та же СЕМЬЯ серии ИЛИ серию не
    # извлекли ИЛИ фильтр is_compressor отсёк). Разные семьи (СБ4 vs КС3) = разные товары, не промах.
    by_o={}     # (b,oururl)->(o, best twin, reason)
    for b in ours_all:
        for o in ours_all[b]:
            if not (o["kw"] and o["bar"] and o["fl"]): continue
            already=matched_urls.get((b,o["url"]), set())
            ofam=o["sn"][0] if o.get("sn") else None
            for dk in (0,-1,1):
                for dbar in (0,-1,1):
                    for c in idx.get((round(o["kw"])+dk, round(o["bar"])+dbar), []):
                        if c["brand"]!=b or c["url"] in already: continue
                        if not (abs(o["kw"]-c["kw"])<=TOL_KW*max(o["kw"],c["kw"])
                                and abs(o["bar"]-c["bar"])<=TOL_BAR*max(o["bar"],c["bar"])
                                and abs(o["fl"]-c["fl"])<=TOL_FL*max(o["fl"],c["fl"])): continue
                        cfam=c["ser"][0] if c["ser"] else None
                        if not c["iscomp"]:           rs="фильтр отсёк конкурента"
                        elif cfam is None:            rs="серия конкурента не извлечена"
                        elif ofam and cfam==ofam:     rs="та же серия — почему не сцепилось?"
                        else: continue                # разные семьи серий = разные товары
                        key=(b,o["url"])
                        if key not in by_o: by_o[key]=(o,c,rs)
    print(f"\nНАШИ товары с РЕАЛЬНЫМ спек-двойником-промахом (того же бренда+семьи): {len(by_o)}")
    print("причины:", dict(Counter(r for _,_,r in by_o.values())))
    print("\nпо брендам (где искать пропуски):")
    for b,n in Counter(b for (b,_) in by_o).most_common(20): print(f"  {b:<14} {n}")
    print("\n=== выборка «та же серия, но не сцепилось» (приоритет) ===")
    shown=0
    for (b,ou),(o,c,rs) in by_o.items():
        if rs.startswith("та же серия") and shown<20:
            print(f"  [{b}] {o['name'][:44]!r} kw{o['kw']}/б{o['bar']}/{o['fl']:g} (sn {o['sn']})")
            print(f"        <> [{c['site']}] {c['name'][:46]!r} kw{c['kw']:g}/б{c['bar']:g}/{c['fl']:g} (sn {c['ser']})")
            shown+=1
    print("\n=== выборка «серия конкурента не извлечена» ===")
    shown=0
    for (b,ou),(o,c,rs) in by_o.items():
        if rs=="серия конкурента не извлечена" and shown<12:
            print(f"  [{b}] {o['name'][:40]!r} <> [{c['site']}] {c['name'][:50]!r}")
            shown+=1

if __name__=="__main__":
    build()
