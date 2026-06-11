"""«Проверить карточку»: наш товар, возможно, описан не по паспортным данным бренда.
Критерий: ≥2 карточки конкурентов СОГЛАСНЫ между собой по величине (кВт/бар/произв.),
а наше значение отличается сверх допуска. Допуски: кВт 6%, бар 10% (v-p-k округляет
7,5->7 — не шум), произв. 4%. Ссылка — на согласную карточку конкурента (подтверждение)."""
import csv, sys
csv.field_size_limit(sys.maxsize)
from spec_match import num, sane_kw, flow_value, bar_value
from atlas_need_specs import load_universe
from matcher import domain
from scrape_files import U

SPECS_CSV = U + "specs2/specs_compact.csv"
_OUR=None; _COMP=None

def _our():
    global _OUR
    if _OUR is None:
        _OUR={}
        for r in csv.DictReader(open(SPECS_CSV, encoding="utf-8-sig", errors="replace"),
                                delimiter=";"):
            code=(r.get("IE_CODE") or "").strip().lower()
            if not code: continue
            cur=_OUR.setdefault(code, {})
            for k in ("IP_PROP22562","IP_PROP22573","IP_PROP22571","IP_PROP22658"):
                if r.get(k) and not cur.get(k): cur[k]=r[k]
    return _OUR

def _comp():
    global _COMP
    if _COMP is None:
        _, specs, _ = load_universe()
        _COMP={}
        for u,d in specs.items():
            kw=bar=fl=None
            for k,v in d.items():
                kl=k.lower()
                if kw is None and "мощ" in kl and "шум" not in kl and "звук" not in kl:
                    kw=sane_kw(num(v))
                if bar is None and "давлен" in kl: bar=bar_value(v)
                if fl is None and "произв" in kl: fl=flow_value(v, kl+" "+str(v))
            if kw or bar or fl: _COMP[u]=(kw,bar,fl)
    return _COMP

def _ourvals(our_url):
    r=_our().get(str(our_url).rstrip("/").split("/")[-1].lower())
    if not r: return None
    return (sane_kw(num(r.get("IP_PROP22562"))),
            bar_value(r.get("IP_PROP22573")),
            flow_value(r.get("IP_PROP22571"), "л/мин") or flow_value(r.get("IP_PROP22658"), "м3/мин"))

FIELDS=[("кВт",0,0.06), ("бар",1,0.10), ("произв",2,0.04)]

def check_card(our_url, comp_urls):
    """-> (текст «бар: у нас 10, у конкурентов 13 (2 ист.)», url-подтверждение) | None."""
    ours=_ourvals(our_url)
    if not ours: return None
    comp=_comp()
    for label,idx,tol in FIELDS:
        ov=ours[idx]
        if not ov: continue
        vals=[(u, comp[u][idx]) for u in comp_urls if u in comp and comp[u][idx]]
        for u1,v1 in vals:
            agree=[(u2,v2) for u2,v2 in vals if abs(v2-v1)<=tol*max(v2,v1)]
            doms={domain(u2) for u2,_ in agree}        # консенсус ≥2 РАЗНЫХ сайтов (не клоны)
            if len(doms)>=2 and abs(ov-v1)>tol*max(ov,v1):
                return (f"{label}: у нас {ov:g}, у конкур. {v1:g} ({len(doms)} сайт.)", agree[0][0])
    return None
