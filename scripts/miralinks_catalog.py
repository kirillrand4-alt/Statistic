"""Pull the Miralinks donor catalog into ``donor_site`` (guest-post база).

Miralinks has no public API; we replay the catalog's own DataTables AJAX request
with your logged-in session cookie, paging through the whole filtered set. Capture
once from the browser (DevTools -> the ``loadDataTableDataCatalog`` request):
  * the session **cookie** -> save in Настройки (``miralinks_cookie``) or --cookie-file
  * the request **body** (Payload, "view source") -> Настройки (``miralinks_body``) or --body-file
The body's ``searchData`` holds your filters. You can re-capture it from the
browser, or edit the filter fields right here from the console (--show-body /
--set-filter) — no browser needed once a body is saved.

Примеры (весь путь из консоли):
    # 0. один раз сохранить cookie и тело запроса из файлов в Настройки
    python scripts/miralinks_catalog.py --save-cookie cookie.txt
    python scripts/miralinks_catalog.py --save-body body.txt
    # 1. посмотреть текущие фильтры (декодированный searchData) — узнать имена полей
    python scripts/miralinks_catalog.py --show-body
    # 2. выставить фильтры по именам полей из шага 1 (пример под ИКС/MR):
    python scripts/miralinks_catalog.py \
        --set-filter sqiFrom=3860 --set-filter sqiTo=5550 \
        --set-filter mrFrom=100 --set-filter mrTo=100
    # 3. очистить базу (upsert старое не удаляет) и вытянуть по новому фильтру
    python scripts/miralinks_catalog.py --wipe
    python scripts/miralinks_catalog.py --apply
    # проба одной страницы / выгрузка сохранённого каталога
    python scripts/miralinks_catalog.py --probe
    python scripts/miralinks_catalog.py --dump donors.csv
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.credentials import get_cred, set_cred  # noqa: E402
from app.db.base import SessionLocal, init_db  # noqa: E402
from app.providers.miralinks import (  # noqa: E402
    Miralinks, get_search_data, parse_rows, patch_search_data, total_records,
)
from app.services import donors as D  # noqa: E402


def _read(path: str | None, cred_key: str) -> str | None:
    if path:
        # utf-8-sig strips a BOM that Windows Notepad prepends — a leading BOM
        # would corrupt the first request param and make Miralinks 500.
        with open(path, encoding="utf-8-sig") as fh:
            return fh.read().strip()
    return get_cred(cred_key)


def probe(cookie: str, body: str, length: int) -> None:
    client = Miralinks(cookie, body)
    payload = client.fetch_page(0, length)
    if payload.get("error"):
        print("Ошибка:", payload.get("detail") or payload.get("error"),
              "| status:", payload.get("status"))
        if payload.get("raw"):
            print("  Сырой ответ (обрезан):", payload["raw"])
        return
    total = total_records(payload)
    rows = list(parse_rows(payload))
    print(f"Всего под фильтром: {total}; на странице: {len(rows)}")
    for r in rows[:10]:
        print(f"  {r['domain']:<28} ИКС={r['sqi']} DR={r['ahrefs_dr']} "
              f"цена={r['price_rur']}₽ трафик={r['traffic']} [{r['region']}] {r['topics']}")
    if total == 0 and not rows:
        keys = list(payload.keys())
        snippet = json.dumps(payload, ensure_ascii=False)[:600]
        looks_logged_out = not any(k in payload for k in
                                   ("aaData", "iTotalRecords", "iTotalDisplayRecords"))
        print("  ⚠ Ноль результатов. Ключи ответа:", keys)
        print("  Сырой ответ (обрезан):", snippet)
        if looks_logged_out:
            print("  → Ответ без данных каталога — cookie почти наверняка протух. Обновите его:\n"
                  "     скопируйте свежий cookie из DevTools → сохраните: "
                  "python scripts\\miralinks_catalog.py --save-cookie cookie.txt")
        else:
            print("  → Ответ валиден, но каталог вернул 0 — либо cookie протух (сессия отдаёт пусто),\n"
                  "     либо фильтры слишком строгие. Ослабьте по одному, напр.:\n"
                  "     python scripts\\miralinks_catalog.py --set-filter s_placement_time= "
                  "--set-filter s_venality= --set-filter s_maxSpamness=\n"
                  "     и снова --probe. Если и без фильтров 0 — точно обновляйте cookie.")


def show_body(body: str) -> None:
    """Print the saved catalog request: its size and the decoded ``searchData``
    filters (so you can see the exact field names to pass to --set-filter)."""
    sd = get_search_data(body)
    print(f"Тело запроса: {len(body)} символов.")
    if not sd:
        print("searchData не найден или не разобран — тело нестандартное; "
              "перекопируйте запрос (Payload → view source) целиком.")
        return
    print(f"Фильтры (searchData), {len(sd)} полей:")
    print(json.dumps(sd, ensure_ascii=False, indent=2))


def set_filters(body: str, pairs: list[str], out: str | None) -> None:
    """Apply ``KEY=VALUE`` overrides to the body's searchData, then persist.

    Empty value (``KEY=``) removes the field. Saves to --out file if given,
    otherwise back into Настройки (``miralinks_body``)."""
    updates: dict = {}
    for p in pairs:
        if "=" not in p:
            sys.exit(f"Неверный --set-filter «{p}» — нужен вид ИМЯ=ЗНАЧЕНИЕ.")
        k, v = p.split("=", 1)
        updates[k.strip()] = (v if v != "" else None)  # пусто → удалить поле
    new_body = patch_search_data(body, updates)
    if get_search_data(body) == get_search_data(new_body) and not updates:
        print("Нет изменений.")
        return
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(new_body)
        print(f"Сохранено в файл: {out}")
    else:
        set_cred("miralinks_body", new_body)
        print("Тело запроса с новыми фильтрами сохранено в Настройки (miralinks_body).")
    print("Проверка нового searchData:")
    print(json.dumps(get_search_data(new_body), ensure_ascii=False, indent=2))


def wipe() -> None:
    init_db()
    db = SessionLocal()
    try:
        n = D.wipe(db)
        print(f"База доноров очищена: удалено {n} строк.")
    finally:
        db.close()


def dump(path: str) -> None:
    init_db()
    db = SessionLocal()
    try:
        rows = D.donor_rows(db, sort="sqi", limit=None)
        fmt = "xlsx" if path.lower().endswith(".xlsx") else "csv"
        _, buf, _ = D.build_export(rows, fmt=fmt)
        with open(path, "wb") as fh:
            fh.write(buf.getvalue())
        print(f"Выгружено строк: {len(rows)} → {path}")
    finally:
        db.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookie-file", help="файл с cookie сессии (иначе из Настроек)")
    ap.add_argument("--body-file", help="файл с телом запроса (иначе из Настроек)")
    ap.add_argument("--length", type=int, default=100, help="площадок за один запрос")
    ap.add_argument("--pause", type=float, default=1.5, help="пауза между страницами, сек")
    ap.add_argument("--max", dest="max_records", type=int, default=0, help="лимит площадок (0=все)")
    ap.add_argument("--probe", action="store_true", help="только проверить (1 страница)")
    ap.add_argument("--apply", action="store_true", help="реально тянуть и сохранять")
    ap.add_argument("--dump", help="выгрузить сохранённый каталог в этот CSV/XLSX и выйти")
    ap.add_argument("--wipe", action="store_true",
                    help="удалить ВСЕ сохранённые доноры (перед сбором с новыми фильтрами) и выйти")
    # console-only управление cookie/телом/фильтрами (без браузера)
    ap.add_argument("--save-cookie", metavar="FILE", help="сохранить cookie из файла в Настройки и выйти")
    ap.add_argument("--save-body", metavar="FILE", help="сохранить тело запроса из файла в Настройки и выйти")
    ap.add_argument("--show-body", action="store_true",
                    help="показать сохранённый запрос и его фильтры (searchData) и выйти")
    ap.add_argument("--set-filter", action="append", default=[], metavar="ИМЯ=ЗНАЧ",
                    help="изменить поле фильтра в searchData (повторяемо; ИМЯ= удаляет поле)")
    ap.add_argument("--out", metavar="FILE",
                    help="куда писать результат --set-filter (по умолч. — в Настройки)")
    a = ap.parse_args()

    if a.save_cookie:
        set_cred("miralinks_cookie", _read(a.save_cookie, "").strip())
        print("Cookie сохранён в Настройки.")
        return
    if a.save_body:
        body = (_read(a.save_body, "") or "").strip()
        if "searchData=" not in body:
            print("⚠ В теле нет searchData= — похоже, Payload скопирован НЕ в режиме «view source»\n"
                  "  (скопировался разобранный вид, а не сырая строка). Рабочее тело НЕ перезаписано.\n"
                  "  В DevTools → вкладка Payload → нажми «view source» → скопируй ВСЮ строку целиком\n"
                  "  (в ней должно быть …&searchData=%7B…%7D) и повтори --save-body.")
            sys.exit(1)
        set_cred("miralinks_body", body)
        print(f"Тело запроса сохранено в Настройки ({len(body)} символов).")
        return
    if a.wipe:
        wipe()
        return
    if a.dump:
        dump(a.dump)
        return

    if a.show_body:
        body = _read(a.body_file, "miralinks_body")
        if not body:
            sys.exit("Нет сохранённого тела запроса (--body-file или Настройки).")
        show_body(body)
        return
    if a.set_filter:
        body = _read(a.body_file, "miralinks_body")
        if not body:
            sys.exit("Нет тела запроса для правки (--body-file или Настройки).")
        set_filters(body, a.set_filter, a.out)
        return

    cookie = _read(a.cookie_file, "miralinks_cookie")
    body = _read(a.body_file, "miralinks_body")
    if not cookie or not body:
        print("Нужны cookie и тело запроса (--cookie-file/--body-file или Настройки).")
        sys.exit(1)

    if a.probe or not a.apply:
        probe(cookie, body, a.length)
        if not a.apply:
            print("Это проба. Добавьте --apply, чтобы вытянуть и сохранить весь каталог.")
        return

    res = D.run_catalog(cookie, body, length=a.length, pause=a.pause,
                        max_records=a.max_records, log=lambda m: print(m, flush=True))
    if res.get("error"):
        sys.exit(2)


if __name__ == "__main__":
    main()
