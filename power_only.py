"""Power-only матчинг: товары БЕЗ серии в названии (Airpol 11/10, ZUV 11, MIG 11…).
Пул: обе стороны с ser_of()==None (в основной пайплайн не входят — двойного счёта нет).
Правило (пилот airpol/zuv/mig, взаимность 0): бренд + kw РАВНО + bar РАВНО + fl ±2% +
vsd/ff строго + IP-класс равен (если оба) + привод из текста СТРОГО равен (молчание ≠
«ременной») + вариант-токены имён (К, PR, D…) совпадают + receiver_filter.
Результат идёт на отдельный лист «Особо проверить (нет серии)» в брендовых отчётах."""
import csv, sys, re
csv.field_size_limit(sys.maxsize)
from collections import defaultdict, Counter
from matcher import brand_of, brand_from_text, BRAND_ALIASES
from spec_match import (is_compressor, num, sane_kw, bar_value, bar_from_text, flow_value,
                        text_flags, receiver_filter)
from brand_spec_review import SPECS_CSV, PROKO_CSV, ser_of, _CYR2LAT
from atlas_need_specs import load_universe, is_product_url, slug, dm, COMPETITORS
from dropped_export import specs_kbf

def ip_of(text):
    m = re.search(r'ip\s*[- ]?(\d{2})', str(text).lower())
    return m.group(1) if m else None

def drive_of_text(name):
    t = str(name).lower()
    if "ремен" in t: return "ремен"
    if "прям" in t:  return "прямой"
    return None

_VAR_NOISE = {"ip", "л", "l", "квт", "kw", "м3", "лс", "hp", "v", "в", "с", "и", "на"}
def variant_sig(name, brand):
    """Короткие альфа-токены (≤2 симв.) = вариант-маркеры (К=исполнение, PR=прямой привод,
    D/F/Е). Бренд-алиасы и единицы исключены. CYR→LAT. Пилот: ловит MIG К и MIG PR."""
    btoks = {brand} | {a for a, c in BRAND_ALIASES.items() if c == brand}
    out = set()
    for t in re.split(r"[^a-zа-яё+]+", str(name).lower()):
        t = t.translate(_CYR2LAT)
        if not t or len(t) > 2 or t in _VAR_NOISE or t in btoks: continue
        out.add(t)
    return frozenset(out)

def load_ours_po(brands=None):
    """Наши компрессоры с брендом, но БЕЗ извлекаемой серии. Бренд — с фолбэком по
    названию (ZUV: производитель в Битриксе не распознаётся, бренд только в имени)."""
    rows = {}
    for r in csv.DictReader(open(SPECS_CSV, encoding="utf-8-sig", errors="replace"), delimiter=";"):
        code = (r.get("IE_CODE") or "").strip()
        if not code: continue
        cur = rows.setdefault(code, {})
        for k, v in r.items():
            if v and not cur.get(k): cur[k] = v.strip()
    price = {}
    for row in csv.reader(open(PROKO_CSV, encoding="utf-8-sig", errors="replace"), delimiter=";"):
        if len(row) < 3 or "prokompressor" not in row[1]: continue
        sl = row[1].rstrip("/").split("/")[-1].lower()
        try: p = float(str(row[2]).replace(",", ".").replace(" ", "")) or None
        except: p = None
        price[sl] = (row[0].strip().replace("&quot;", '"'), row[1].strip(), p)
    out = defaultdict(list)
    for code, r in rows.items():
        man = (r.get("IP_PROP22553") or "").strip()
        name = r.get("IE_NAME", "")
        b = (brand_from_text(man) or BRAND_ALIASES.get(man.lower().split()[0] if man else "", None)
             or brand_from_text(name))
        if not b or (brands and b not in brands): continue
        text = name + " " + code
        if not is_compressor(text): continue
        if ser_of(text, b): continue                 # есть серия — обычный пайплайн, не сюда
        ff, vsd, rv = text_flags(text)
        if rv is None:
            if num(r.get("IP_PROP22564")): rv = num(r.get("IP_PROP22564"))
            elif str(r.get("IP_PROP22574", "")).strip().lower() in ("да", "есть"): rv = 1
        nm, url, p = price.get(code.lower(), (name, f"https://prokompressor.ru/catalog/{code}/", None))
        out[b].append(dict(
            name=nm or name, url=url, price=p,
            kw=sane_kw(num(r.get("IP_PROP22562"))),
            bar=bar_value(r.get("IP_PROP22573")) or bar_from_text(text),
            fl=flow_value(r.get("IP_PROP22571"), "л/мин") or flow_value(r.get("IP_PROP22658"), "м3/мин"),
            ff=ff, vsd=vsd, rv=rv, ip=ip_of(text)))
    return out

def load_comp_po(brands=None):
    """Карточки конкурентов без извлекаемой серии + цена/статус из выгрузок парсера."""
    names, specs, _ = load_universe()
    price = {}; status = {}
    import scrape_files
    for f in scrape_files.SCRAPE_FILES:
        try: fh = open(f, encoding="utf-8-sig", errors="replace")
        except FileNotFoundError: continue
        for r in csv.DictReader(fh):
            u = (r.get("product_url") or "").strip()
            if not u: continue
            try:
                v = float(str(r.get("price", "")).replace(",", ".").replace(" ", ""))
                if 100 <= v <= 50_000_000: price[u] = v
            except: pass
            st = (r.get("series_status") or "").lower()
            if "снят" in st and "v-p-k.ru/catalog" not in u: status[u] = "снято"
    out = defaultdict(list)
    for u, nm in names.items():
        if dm(u) not in COMPETITORS: continue
        if not is_product_url(u): continue
        b = brand_of(u, nm)
        if not b or (brands and b not in brands): continue
        text = (nm or "") + " " + slug(u)
        if not is_compressor(text): continue
        if ser_of(text, b): continue
        kw, bar, fl = specs_kbf(specs.get(u, {}))
        ff, vsd, rv = text_flags(text)
        out[b].append(dict(name=nm or slug(u), url=u, site=dm(u), kw=kw, bar=bar, fl=fl or None,
                           ff=ff, vsd=vsd, rv=rv, ip=ip_of(text),
                           price=price.get(u), status=status.get(u, "")))
    return out

def po_match(o, cands, brand):
    """ВСЕ три спеки обязательны и равны (fl ±2%); vsd/ff строго; IP равен, если оба;
    привод из текста СТРОГО равен; вариант-токены имён совпадают; receiver направленно."""
    if None in (o.get("kw"), o.get("bar"), o.get("fl")): return []
    ov = variant_sig(o["name"], brand)
    od = drive_of_text(o["name"])
    res = []
    for c in cands:
        if None in (c.get("kw"), c.get("bar"), c.get("fl")): continue
        if o["kw"] != c["kw"] or o["bar"] != c["bar"]: continue
        if abs(o["fl"] - c["fl"]) > 0.02 * max(o["fl"], c["fl"]): continue
        if bool(o.get("vsd")) != bool(c.get("vsd")): continue
        if bool(o.get("ff")) != bool(c.get("ff")): continue
        if o.get("ip") and c.get("ip") and o["ip"] != c["ip"]: continue
        if drive_of_text(c["name"]) != od: continue
        if variant_sig(c["name"], brand) != ov: continue
        res.append(c)
    return receiver_filter(o.get("rv"), res)

def build_po_pairs(brands=None):
    """{brand: [(наш, [карточки])]} — для листа «Особо проверить (нет серии)»."""
    ours = load_ours_po(brands)
    comps = load_comp_po(brands)
    out = {}
    for b in sorted(set(ours) & set(comps)):
        pairs = []
        for o in ours[b]:
            m = po_match(o, comps[b], b)
            if m: pairs.append((o, m))
        if pairs: out[b] = pairs
    return out

if __name__ == "__main__":
    # пилот-режим с детекторами ложных срабатываний
    PILOT = set(sys.argv[1:]) or {"airpol", "zuv", "mig"}
    ours = load_ours_po(PILOT); comps = load_comp_po(PILOT)
    for b in sorted(PILOT):
        O, C = ours.get(b, []), comps.get(b, [])
        matched = pairs = 0
        recip = defaultdict(set)
        for o in O:
            m = po_match(o, C, b)
            if not m: continue
            matched += 1; pairs += len(m)
            key = re.sub(r"\s+", " ", o["name"].strip().lower())
            for c in m: recip[c["url"]].add(key)
        bad = {u: ks for u, ks in recip.items() if len(ks) > 1}
        print(f"[{b}] наших po: {len(O)}, конк po: {len(C)}, матчей: {matched}, пар: {pairs}, "
              f"взаимность>1: {len(bad)}")
        for u, ks in list(bad.items())[:4]:
            print(f"   {u[:75]}")
            for k in sorted(ks)[:3]: print(f"     <- {k[:65]}")
