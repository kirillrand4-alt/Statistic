"""Где лежат входные данные матчера: выгрузки парсера и наш каталог.

Раньше здесь был прибит гвоздями путь к папке загрузок конкретной сессии
(`/root/.claude/uploads/62a19005-.../`) и перечислены двадцать файлов поимённо.
Папка живёт ровно одну сессию, поэтому при каждом новом заходе матчер переставал
видеть данные, а свежие выгрузки приходилось дописывать в список руками.

Теперь путь задаётся снаружи, а файлы подхватываются сами:

    MATCH_DATA_DIR   — корень с данными (по умолчанию ./data рядом со скриптами)
    PARSER_CSV_DIR   — выгрузки парсера   (по умолчанию MATCH_DATA_DIR/parser)
    OURS_DIR         — наш каталог из Битрикса (по умолчанию MATCH_DATA_DIR/ours)

Пример (Windows, парсер на этой же машине):

    set MATCH_DATA_DIR=C:\\parser\\data
    set PARSER_CSV_DIR=C:\\parser\\data

Порядок файлов ХРОНОЛОГИЧЕСКИЙ — по метке времени в имени
(`prices_20260811_015930.csv`). При объединении характеристик по URL поздний
прогон переписывает ключи, поэтому порядок важен.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent

DATA_DIR = Path(os.getenv("MATCH_DATA_DIR") or (_HERE / "data"))
PARSER_CSV_DIR = Path(os.getenv("PARSER_CSV_DIR") or (DATA_DIR / "parser"))
OURS_DIR = Path(os.getenv("OURS_DIR") or (DATA_DIR / "ours"))

# Совместимость: десяток скриптов берёт отсюда базовый путь как строку и
# приклеивает к нему имя файла. Оставляем `U` с завершающим разделителем.
U = str(DATA_DIR) + os.sep

# Имена парсера: prices_<дата>_<время>.csv и prices_checked_<дата>.csv.
# Старые выгрузки назывались all_prices_<дата>_<время>.csv — их тоже берём.
_TS = re.compile(r"(?:all_)?prices(?:_checked)?_(\d{8})(?:_(\d{6}))?", re.I)


def _stamp(path: Path) -> str:
    """Метка времени из имени файла — по ней сортируем хронологически."""
    m = _TS.search(path.name)
    if not m:
        return "00000000_000000"
    return f"{m.group(1)}_{m.group(2) or '000000'}"


def find_scrape_files(directory: Path | str | None = None) -> list[str]:
    """Все выгрузки парсера из папки, от ранних к поздним."""
    d = Path(directory) if directory else PARSER_CSV_DIR
    if not d.is_dir():
        return []
    files = [p for p in d.glob("*.csv") if _TS.search(p.name)]
    return [str(p) for p in sorted(files, key=_stamp)]


SCRAPE_FILES: list[str] = find_scrape_files()

# Прогоны обновлённого парсера (фиксы проверены с 06:01 10.06, см. PARSER_NOTES).
# Если URL сканирован новым парсером и поле всё равно пустое — на странице
# данных нет, повторный прогон не поможет.
_NEW_PARSER_FROM = os.getenv("NEW_PARSER_FROM", "20260610_060124")
NEW_PARSER_FILES: list[str] = [f for f in SCRAPE_FILES
                               if _stamp(Path(f)) >= _NEW_PARSER_FROM]


def find_ours(part: str, legacy: str = "") -> str:
    """Файл нашего каталога по куску имени: самый свежий из OURS_DIR.

    Имена выгрузок Битрикса меняются от раза к разу
    (`products_export_20260608.csv`, `..._20260711.csv`), поэтому ищем по
    подстроке и берём последний по времени изменения. `legacy` — путь, который
    был прибит гвоздями раньше: возвращаем его, если ничего не нашли, чтобы
    ошибка была понятной («нет такого файла»), а не «переменная не задана».
    """
    if OURS_DIR.is_dir():
        found = [p for p in OURS_DIR.rglob("*.csv") if part.lower() in p.name.lower()]
        if found:
            return str(max(found, key=lambda p: p.stat().st_mtime))
    return U + legacy if legacy else ""


def describe() -> str:
    """Короткая сводка — чтобы скрипты могли печатать, что именно нашли."""
    if not SCRAPE_FILES:
        return (f"выгрузок парсера не найдено в {PARSER_CSV_DIR}\n"
                f"  задайте PARSER_CSV_DIR или положите prices_*.csv туда")
    first, last = Path(SCRAPE_FILES[0]).name, Path(SCRAPE_FILES[-1]).name
    return (f"выгрузок парсера: {len(SCRAPE_FILES)} из {PARSER_CSV_DIR}\n"
            f"  от {first} до {last} (новым парсером: {len(NEW_PARSER_FILES)})")


if __name__ == "__main__":
    print(describe())
    print(f"наш каталог ожидается в: {OURS_DIR}")
