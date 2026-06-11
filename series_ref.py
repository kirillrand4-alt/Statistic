"""Справочник актуальности серий (LLM-проверка, файл пользователя bc49ef49):
снятые/архивные серии брендов + URL-подтверждение. Колонка «Серия снята (подтв.)»
в отчётах. Короткие серии (<3 знаков, напр. ARIACOM 'AR') не матчим — ложные на бренд."""
import openpyxl, re
from collections import defaultdict
from matcher import brand_from_text, BRAND_ALIASES

SERIES_FILE = ("/root/.claude/uploads/62a19005-a7bf-569b-926e-b59b4a62600d/"
               "bc49ef49-______________________.xlsx")

def _norm(s): return re.sub(r"[^a-zа-я0-9]", "", str(s).lower())

def _canon(brand):
    b=str(brand).strip().lower()
    return (BRAND_ALIASES.get(b) or brand_from_text(b)
            or BRAND_ALIASES.get(b.split()[0], b.split()[0] if b else "?"))

_REF=None
def load_discontinued():
    """brand_canon -> [(серия, norm, url-подтверждение)], длинные серии первыми."""
    global _REF
    if _REF is not None: return _REF
    out=defaultdict(list)
    try:
        ws=openpyxl.load_workbook(SERIES_FILE, read_only=True, data_only=True)["Лист1"]
        for r in ws.iter_rows(min_row=2, values_only=True):
            st=str(r[8] or "").lower()
            if "снят" not in st and "архив" not in st: continue
            ser=str(r[1] or "").strip()
            if len(_norm(ser))<3: continue
            url=(str(r[10]).strip() if r[10] else "") or \
                (str(r[11]).split("\n")[0].strip() if r[11] else "")
            out[_canon(r[0])].append((ser, _norm(ser), url))
        for b in out: out[b].sort(key=lambda t:-len(t[1]))
    except FileNotFoundError:
        pass
    _REF=dict(out)
    return _REF

def snyataya_seriya(brand, name):
    """Если товар принадлежит снятой серии бренда -> (серия, url) иначе None."""
    nn=_norm(name)
    for ser, ns, url in load_discontinued().get(brand, ()):
        if ns in nn: return ser, url
    return None
