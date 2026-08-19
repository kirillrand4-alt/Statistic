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
        "IP_PROP22555",
        # Класс защиты двигателя: 22674 «IP электродвигателя» + 22959 «Степень защиты
        # двигателя» (два свойства об одном, берём оба — load_ours_all найдёт колонку по
        # значению вида «IP54»). Добавлены 12.08: агенты по живым страницам доказали, что
        # у CrossAir/Hansmann/Berg IP23 и IP54/55 — разные SKU (6 ложных пар из 11), а в
        # компакте класса защиты не было вовсе и ip_filter молчал с нашей стороны.
        "IP_PROP22674", "IP_PROP22959",
        # Двигатель дизельных станций: 22569 «Производитель двигателя», 23013 «Модель
        # двигателя». Добавлены 13.08: ЗИФ-ПВ-16/0,7 с Д-260 ММЗ сцеплялся с их
        # исполнением на ЯМЗ — заводы дают разные SKU под разные моторы.
        "IP_PROP22569", "IP_PROP23013",
        # «Габариты, мм». Добавлены 18.08: в PROPS_BITRIX они значились служебными и
        # «для сцепки не нужны» — вывод отозван. Все пять раундов проверки агентами по
        # живым карточкам решались одной и той же парой улик, весом И габаритами
        # («вес 1350 кг и 2350х1250х1880 совпали до килограмма и миллиметра» — Spitzenreiter
        # SZW55AF = наш Dali VFW55-8F). Вес в компакте был, габаритов не было вовсе.
        "IP_PROP22556",
        # Осушители (19.08). До этого захода осушители не доходили до матчера вовсе —
        # их резал is_compressor, и в компакте не было ни одного их профильного поля.
        # 23035 «Пропускная способность, л/мин» — основное число осушителя, аналог
        # производительности у компрессора: заполнено у 2 675 из 2 795 карточек. 22572
        # — свойство С ТЕМ ЖЕ названием и кодом PROPUSKNAYA_SPOSOBNOST_L_MIN_1/без «_1»,
        # заполнено всего у 140: берём как фолбэк, а не как основное (легко перепутать).
        # 22585 «Точка росы, °C» (2 377) — второй ключевой признак: -40 против -20 это
        # разные машины при одной пропускной способности.
        "IP_PROP23035", "IP_PROP22572", "IP_PROP22585",
        # Тип осушителя: 22584 «Тип хладагента» (952, у адсорбционных туда пишут сорбент
        # — «силикагель», «молекулярное сито») и 22724 «Тип адсорбционного осушителя»
        # (399: колонный/модульный). Заполнены неровно, поэтому только как направленный
        # признак («молчание = совместимо»), не как обязательное совпадение.
        "IP_PROP22584", "IP_PROP22724"]

# ПРОВЕРЕНО И НЕ ВЗЯТО (19.08): 22664 «Исполнение» и 22680 «Тип ресивера» — у осушителей
# и ресиверов не заполнены НИ РАЗУ (0 из 2 795 и 0 из 312), заполнение по каталогу целиком
# (232 и 7) приходится на другие категории. Ресиверам новых полей не нужно вовсе: объём
# (22564), давление, вес и габариты в KEEP уже были.


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
