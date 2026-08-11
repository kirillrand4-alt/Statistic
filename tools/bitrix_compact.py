"""Собрать specs_compact.csv из полного экспорта каталога Битрикса.

Экспорт отдаёт ПО СТРОКЕ НА ЗНАЧЕНИЕ СВОЙСТВА, поэтому весит гигабайты:
600 738 строк на 27 434 товара. Матчер схлопывает их по IE_CODE, беря первое
непустое значение каждого поля — делаем то же самое на лету, читая прямо из
tar.gz, без разворачивания на диск (~50 секунд на 4.2 ГБ).

    python tools/bitrix_compact.py bitrix-export-full.tar.gz -o data/ours/specs_compact.csv
    python tools/bitrix_compact.py export.csv                # можно и голый CSV

Расшифровка колонок IP_PROP — в PROPS_BITRIX.md.
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import tarfile
import time
from pathlib import Path

csv.field_size_limit(10**9)

# Ровно те поля, которые читает load_ours_all(). Брать больше незачем: каждое
# лишнее поле — это мегабайты, а на сцепку они не влияют (см. PROPS_BITRIX.md,
# раздел «Проверено и НЕ используется»).
KEEP = ["IE_CODE", "IE_NAME", "IE_ID", "IE_ACTIVE",
        "IP_PROP22553", "IP_PROP22562", "IP_PROP22571", "IP_PROP22658",
        "IP_PROP22573", "IP_PROP22583", "IP_PROP22586", "IP_PROP22565",
        "IP_PROP22564", "IP_PROP22574", "IP_PROP22601", "IP_PROP22669",
        "IP_PROP22555"]


def open_export(path: Path):
    """Отдать текстовый поток CSV — из tar.gz или из обычного файла."""
    if path.suffix in (".gz", ".tgz") or path.name.endswith(".tar.gz"):
        tf = tarfile.open(path, "r:gz")
        member = next((m for m in tf if m.name.lower().endswith(".csv")), None)
        if member is None:
            sys.exit(f"в архиве {path} нет ни одного .csv")
        return io.TextIOWrapper(tf.extractfile(member), encoding="utf-8-sig",
                                errors="replace")
    return open(path, encoding="utf-8-sig", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", help="bitrix-export-full.tar.gz или export.csv")
    ap.add_argument("-o", "--out", default="specs_compact.csv")
    a = ap.parse_args()

    src = Path(a.export)
    if not src.exists():
        sys.exit(f"нет файла {src}")

    fh = open_export(src)
    rdr = csv.DictReader(fh, delimiter=";")
    missing = [k for k in KEEP if k not in (rdr.fieldnames or [])]
    if missing:
        print(f"внимание: в экспорте нет колонок {missing}", file=sys.stderr)

    rows: dict[str, dict] = {}
    n, t0 = 0, time.time()
    for r in rdr:
        n += 1
        code = (r.get("IE_CODE") or "").strip()
        if not code:
            continue
        cur = rows.setdefault(code, {})
        for k in KEEP:
            v = (r.get(k) or "").strip()
            if v and not cur.get(k):
                cur[k] = v
        if n % 2_000_000 == 0:
            print(f"  строк {n:,} | товаров {len(rows):,} | {time.time()-t0:.0f} с",
                  flush=True)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=KEEP, delimiter=";")
        w.writeheader()
        for code, r in rows.items():
            r["IE_CODE"] = code
            w.writerow({k: r.get(k, "") for k in KEEP})

    filled = sum(1 for r in rows.values() if r.get("IP_PROP22553"))
    print(f"строк прочитано {n:,} -> товаров {len(rows):,} за {time.time()-t0:.0f} с")
    print(f"с брендом: {filled:,} ({filled*100//max(len(rows),1)}%)")
    print(f"записано: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
