"""Боевой спек-матч по Zega/Airpol/Airman + список «не совпало, с ценами».
Xlsx как обычный boevoy (build_brand), плюс CSV: все URL без матча, у кого есть цена
(наша сторона и конкуренты), с причиной."""
import csv, sys, os, zipfile
csv.field_size_limit(sys.maxsize)
from collections import defaultdict
import brand_spec_review as B
from spec_match import match, receiver_filter, ff_filter, ip_filter, cool_filter

BRANDS=["zega","airpol","airman"]
B.OUTDIR="/home/user/Statistic/brand3_reports"
ZIP="/home/user/Statistic/Zega_Airpol_Airman_spec_match.zip"
UNM="/home/user/Statistic/Nesovpavshie_s_cenami.csv"

def fmt(v): return "" if v is None else (f"{v:g}" if isinstance(v,float) else str(v))
def fmtp(v): return "" if v is None else f"{float(v):.0f}"

def main():
    os.makedirs(B.OUTDIR, exist_ok=True)
    ours=B.load_ours_all(); cands=B.load_comp_all()
    made=[]; unm=[]
    for b in BRANDS:
        title=b.capitalize()
        r=B.build_brand(b, title, ours.get(b,[]), cands.get(b,[]))   # боевой xlsx (5 листов)
        made.append(r["path"])
        print(f"{title:<8} матч {r['clean']:>3} | неодн {r['ambig']:>3} | без {r['no']:>3} | GAP {r['gap']:>3} | снятые {r['sny']:>3}")
        # --- не совпало, с ценами ---
        by=defaultdict(list)
        for c in cands.get(b,[]): by[c["sn"]].append(c)
        matched=set()
        for o in ours.get(b,[]):
            m=receiver_filter(o.get("rv"), ff_filter(o.get("ff"),
                cool_filter(o.get("cool"), ip_filter(o.get("ip"), match(o, by.get(o["sn"], []))))))
            if m:
                for c in m: matched.add(c["url"])
            elif o.get("price"):                       # наш без матча, цена есть
                reason="серии нет у конкурентов" if not by.get(o["sn"]) else "серия есть, спеки разошлись"
                unm.append([title,"наш",o.get("name",""),f"{o['sn'][0]} {o['sn'][1]:g}",
                            fmtp(o.get("price")),"prokompressor.ru",reason,o["url"]])
        our_sn={o["sn"] for o in ours.get(b,[])}
        seen=set()
        for c in cands.get(b,[]):
            if c["url"] in matched or c["url"] in seen: continue
            seen.add(c["url"])
            if not c.get("price"): continue            # только с ценой
            reason="серии нет у нас" if c["sn"] not in our_sn else "серия есть у нас, спеки разошлись"
            unm.append([title,"конкурент",c.get("name",""),f"{c['sn'][0]} {c['sn'][1]:g}",
                        fmtp(c.get("price")),c.get("site",""),reason,c["url"]])
    with zipfile.ZipFile(ZIP,"w",zipfile.ZIP_DEFLATED) as z:
        for p in made: z.write(p, os.path.basename(p))
    with open(UNM,"w",encoding="utf-8-sig",newline="") as fh:
        w=csv.writer(fh,delimiter=";")
        w.writerow(["бренд","сторона","название","серия","цена,₽","сайт","причина","ссылка"])
        w.writerows(unm)
    print(f"\n«не совпало, с ценами»: {len(unm)} строк -> {UNM}")
    from collections import Counter
    for (br,side),n in sorted(Counter((x[0],x[1]) for x in unm).items()): print(f"   {br:<8} {side:<10} {n}")
    print(f"-> {ZIP}")

if __name__=="__main__":
    main()
