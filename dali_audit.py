"""Аудит матчинга Dali: наш <-> compressortyt. Последовательное добавление характеристик
с проверкой отсева + оценка потерь из-за расхождения разбора серии (наш VFW/DLVF по кВт
vs их DL-<N>, где N бывает м³/мин). Спек-онли матч (kw+bar+fl без серии) = «как должно быть»."""
import csv, sys
sys.path.insert(0,".")
from collections import Counter
from brand_spec_review import load_ours_all, load_comp_all
OUT="/home/user/Statistic/Dali_audit_poteri.csv"

def ag(a,b,t): return a is None or b is None or abs(a-b)<=t*max(a,b)
TOL={"kw":0.06,"bar":0.03,"fl":0.04}

def main():
    ours=load_ours_all().get("dali",[])
    ctt=[c for c in load_comp_all().get("dali",[]) if c["site"]=="compressortyt.ru"]
    print(f"наш Dali={len(ours)} | compressortyt Dali={len(ctt)}\n")

    # --- НАКОПИТЕЛЬНАЯ воронка по парам (наш x ctt) ---
    def ok(o,c,crit):
        if o["sn"]!=c["sn"]: return False
        return all(ag(o.get(k),c.get(k),TOL[k]) for k in crit)
    chain=[("серия (sn)",[]),("+ кВт",["kw"]),("+ бар",["bar"]),("+ произв",["fl"])]
    print("=== НАКОПИТЕЛЬНАЯ воронка (наших товаров с >=1 кандидатом ctt) ===")
    prev=None
    for name,crit in [("серия",[]),("+кВт",["kw"]),("+кВт+бар",["kw","bar"]),("+кВт+бар+произв",["kw","bar","fl"])]:
        have=set(); pairs=0
        for i,o in enumerate(ours):
            for c in ctt:
                if ok(o,c,crit): have.add(i); pairs+=1
        print(f"  {name:<18} наших с матчем: {len(have):>3}/{len(ours)} | пар: {pairs}")

    # --- проверка отсева на шаге +бар: дропы должны иметь РАЗНЫЙ бар ---
    print("\n=== ПРОВЕРКА отсева на +бар (sn+kw прошли, бар отсеял) ===")
    bad=0; checked=0
    for o in ours:
        cs_kw=[c for c in ctt if ok(o,c,["kw"])]
        cs_bar=[c for c in cs_kw if ag(o.get("bar"),c.get("bar"),TOL["bar"])]
        dropped=[c for c in cs_kw if c not in cs_bar]
        for c in dropped:
            checked+=1
            if o.get("bar") is not None and c.get("bar") is not None and o["bar"]==c["bar"]: bad+=1  # отсеян, но бар равен = ошибка
    print(f"  отсеяно баром пар: {checked} | из них с РАВНЫМ баром (ложный отсев): {bad}")

    # --- спек-онли (без серии): сколько РЕАЛЬНО совпадает ---
    print("\n=== СПЕК-ОНЛИ (kw+bar+fl, без серии) — потолок матча ===")
    lost=[]
    series_ok=0
    for o in ours:
        if not (o.get("kw") and o.get("bar") and o.get("fl")): continue
        sp=[c for c in ctt if all(ag(o.get(k),c.get(k),TOL[k]) for k in ("kw","bar","fl"))]
        if not sp: continue
        if any(o["sn"]==c["sn"] for c in sp): series_ok+=1
        else:
            c=sp[0]
            lost.append([o["name"], f"{o['sn'][0]}{o['sn'][1]:g}", o.get("kw"),o.get("bar"),o.get("fl"),
                         c["name"], f"{c['sn'][0]}{c['sn'][1]:g}", c.get("kw"),c.get("bar"),c.get("fl"),
                         o["url"], c["url"]])
    print(f"  спек-матч есть у: {series_ok+len(lost)} наших | из них серия совпала: {series_ok} | ПОТЕРЯНО из-за серии: {len(lost)}")
    with open(OUT,"w",encoding="utf-8-sig",newline="") as fh:
        w=csv.writer(fh,delimiter=";")
        w.writerow(["наш товар","наша серия","кВт","бар","произв","товар ctt","серия ctt","кВт.ctt","бар.ctt","произв.ctt","ссылка наша","ссылка ctt"])
        w.writerows(lost)
    print(f"  -> {OUT} ({len(lost)} потерянных пар)")

if __name__=="__main__":
    main()
