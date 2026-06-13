"""ПОДСВЕТКА почти-матчей: пары наш<->конкурент ОДНОЙ серии, где не сошлась РОВНО ОДНА
спека (все прочие согласны/молчат). Это кандидаты на ручную проверку: скорее всего тот же
товар, но одна характеристика чуть за допуском или размечена иначе.
Конфликт = обе стороны заполнены и расходятся (молчание совместимо, как в боевом match).
Числовое расхождение берём только «близкое» — до NEAR× допуска (далёкое = реально другая
модель). Допуски 1-в-1 с match()/match_cat()."""
import csv, sys, os
csv.field_size_limit(sys.maxsize)
from collections import defaultdict, Counter
from brand_spec_review import load_ours_all, load_comp_all
from category_spec_review import load_ours_cat, load_comp_cat, CATS

OUT="/home/user/Statistic/GAP_почти_матч.csv"
NEAR=2.5   # во сколько раз допуска ещё считаем «почти матч» (4% произв -> до 10%)
CATORD={"компрессор":0,"осушитель":1,"ресивер":2,"генератор азота":3}

def fmt(v): return "" if v is None else (f"{v:g}" if isinstance(v,float) else str(v))
def fmtp(v): return "" if v is None else f"{float(v):.0f}"
def disp(b): return "IngersollRand" if b=="ir" else b.capitalize()

# --- наборы расхождений по типу товара (label, наше, их, kind, ratio=во сколько допусков) ---
def _num(d,label,a,b,tol):
    if a is not None and b is not None and abs(a-b)>tol*max(a,b):
        d.append((label,a,b,"num",abs(a-b)/max(a,b)/tol))
def _abs(d,label,a,b,tol):
    if a is not None and b is not None and abs(a-b)>tol:
        d.append((label,a,b,"num",abs(a-b)/tol))
def _cat(d,label,a,b):
    if a is not None and b is not None and a!=b: d.append((label,a,b,"cat",1.0))

def diffs_comp(o,c):
    d=[]; _num(d,"кВт",o.get("kw"),c.get("kw"),0.06)
    _num(d,"бар",o.get("bar"),c.get("bar"),0.03); _num(d,"произв,л/мин",o.get("fl"),c.get("fl"),0.04)
    _cat(d,"частотник",o.get("vsd"),c.get("vsd")); _cat(d,"привод",o.get("dr"),c.get("dr"))
    _cat(d,"FF/осушитель",o.get("ff"),c.get("ff")); _cat(d,"IP",o.get("ip"),c.get("ip"))
    _cat(d,"охлаждение",o.get("cool"),c.get("cool"))
    return d   # ресивер (rv) намеренно пропущен: флаг vs литры — шумит

def diffs_osush(o,c):
    d=[]; _num(d,"бар",o.get("bar"),c.get("bar"),0.10); _num(d,"произв,л/мин",o.get("fl"),c.get("fl"),0.06)
    _cat(d,"тип",o.get("typ"),c.get("typ")); _abs(d,"точка росы",o.get("dp"),c.get("dp"),5)
    return d
def diffs_resiv(o,c):
    if o.get("vol") is None or c.get("vol") is None: return None   # объём обязателен с обеих -> не «почти»
    d=[]; _num(d,"бар",o.get("bar"),c.get("bar"),0.10); _num(d,"объём,л",o.get("vol"),c.get("vol"),0.02)
    _cat(d,"ориентация",o.get("ori"),c.get("ori"))
    return d
def diffs_azot(o,c):
    d=[]; _num(d,"бар",o.get("bar"),c.get("bar"),0.10); _num(d,"произв,л/мин",o.get("fl"),c.get("fl"),0.06)
    _abs(d,"чистота,%",o.get("pur"),c.get("pur"),0.05)
    return d

def spec_comp(o):
    p=[f"{fmt(o[k])}{u}" for k,u in (("kw","кВт"),("bar","бар"),("fl","л/мин")) if o.get(k) is not None]
    if o.get("ff"): p.append("FF")
    if o.get("vsd"): p.append("VSD")
    return " ".join(p)
def spec_cat(ck,o):
    if ck=="osushiteli": return f"{o.get('typ') or ''} {fmt(o.get('bar'))}бар {fmt(o.get('fl'))}л/мин трТ{fmt(o.get('dp'))}".strip()
    if ck=="resivery": return f"{fmt(o.get('vol'))}л {fmt(o.get('bar'))}бар {o.get('ori') or ''}".strip()
    return f"{fmt(o.get('fl'))}л/мин {fmt(o.get('bar'))}бар чист{fmt(o.get('pur'))}%"

def collect(cat, O, C, difffn, specfn):
    """O,C: brand->[товары]. Возврат строк-кандидатов (ровно 1 расхождение, числовое — близкое)."""
    rows=[]
    for b in set(O)&set(C):
        oby=defaultdict(list); cby=defaultdict(list)
        for o in O[b]: oby[o["sn"]].append(o)
        for c in C[b]: cby[c["sn"]].append(c)
        for sn in set(oby)&set(cby):
            for o in oby[sn]:
                for c in cby[sn]:
                    d=difffn(o,c)
                    if d is None or len(d)!=1: continue
                    label,ov,cv,kind,ratio=d[0]
                    # единственное КАТЕГОРИАЛЬНОЕ расхождение (частотник/привод/IP/тип/ориентация)
                    # = это РАЗНЫЕ товары внутри серии (VSD vs не-VSD и т.п.), а не «почти матч».
                    # Подсвечиваем только числовое и только если оно «близко» (<= NEAR× допуска).
                    if kind!="num" or ratio>NEAR: continue
                    rows.append([cat, disp(b), label, fmt(ov), fmt(cv),
                                 f"{ratio:.1f}×" if kind=="num" else "—",
                                 o.get("name",""), specfn(o), c.get("name",""), specfn(c),
                                 c.get("site",""), o.get("url",""), c.get("url",""), ratio])
    return rows

def build():
    rows=[]
    ours=load_ours_all(); cands=load_comp_all()
    rows+=collect("компрессор", ours, cands, diffs_comp, spec_comp)
    oc=load_ours_cat(); cc=load_comp_cat()
    name={"osushiteli":"осушитель","resivery":"ресивер","azot":"генератор азота"}
    dfn ={"osushiteli":diffs_osush,"resivery":diffs_resiv,"azot":diffs_azot}
    for ck in CATS:
        rows+=collect(name[ck], oc.get(ck,{}), cc.get(ck,{}),
                      dfn[ck], lambda o,ck=ck: spec_cat(ck,o))
    # ближайшие сверху: категория -> разрыв(×допуска) по возр. -> бренд
    rows.sort(key=lambda r:(CATORD.get(r[0],9), r[-1], r[1]))
    HEAD=["категория","бренд","расходится","наше значение","значение конкур","разрыв(×допуска)",
          "наш товар","наши спеки","карточка конкурента","спеки конкур","сайт конкур",
          "наша ссылка","ссылка конкурента"]
    with open(OUT,"w",encoding="utf-8-sig",newline="") as fh:
        wr=csv.writer(fh,delimiter=";"); wr.writerow(HEAD)
        wr.writerows([r[:-1] for r in rows])    # без служебного ratio

    by_cat=Counter(r[0] for r in rows); by_spec=Counter((r[0],r[2]) for r in rows)
    print(f"ПОЧТИ-МАТЧЕЙ (1 спека, числовая <= {NEAR}× допуска): {len(rows)}")
    for cat in CATORD:
        if by_cat[cat]==0: continue
        specs=", ".join(f"{s}:{n}" for (c,s),n in sorted(by_spec.items(),key=lambda x:-x[1]) if c==cat)
        print(f"  {cat:<16} {by_cat[cat]:>5}  ({specs})")
    print(f"-> {OUT}")

if __name__=="__main__":
    build()
