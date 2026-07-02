"""Pull the Miralinks donor catalog into ``donor_site`` (guest-post база).

Miralinks has no public API; we replay the catalog's own DataTables AJAX request
with your logged-in session cookie, paging through the whole filtered set. Capture
once from the browser (DevTools -> the ``loadDataTableDataCatalog`` request):
  * the session **cookie** -> save in Настройки (``miralinks_cookie``) or --cookie-file
  * the request **body** (Payload, "view source") -> Настройки (``miralinks_body``) or --body-file
The body's ``searchData`` holds your filters, so re-capture it to change them.

Examples:
    # dry run: fetch one page and print what would be stored
    python scripts/miralinks_catalog.py --cookie-file c.txt --body-file b.txt --probe
    # full pull (respects pagination + a polite pause between pages)
    python scripts/miralinks_catalog.py --cookie-file c.txt --body-file b.txt --apply
    # wipe the base before re-pulling with different filters (upsert never removes)
    python scripts/miralinks_catalog.py --wipe
    # export the stored catalog to CSV
    python scripts/miralinks_catalog.py --dump donors.csv
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.credentials import get_cred  # noqa: E402
from app.db.base import SessionLocal, init_db  # noqa: E402
from app.providers.miralinks import Miralinks, parse_rows, total_records  # noqa: E402
from app.services import donors as D  # noqa: E402


def _read(path: str | None, cred_key: str) -> str | None:
    if path:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    return get_cred(cred_key)


def probe(cookie: str, body: str, length: int) -> None:
    client = Miralinks(cookie, body)
    payload = client.fetch_page(0, length)
    if payload.get("error"):
        print("Ошибка:", payload.get("detail") or payload.get("error"),
              "| status:", payload.get("status"))
        return
    rows = list(parse_rows(payload))
    print(f"Всего под фильтром: {total_records(payload)}; на странице: {len(rows)}")
    for r in rows[:10]:
        print(f"  {r['domain']:<28} ИКС={r['sqi']} DR={r['ahrefs_dr']} "
              f"цена={r['price_rur']}₽ трафик={r['traffic']} [{r['region']}] {r['topics']}")


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
    a = ap.parse_args()

    if a.wipe:
        wipe()
        return

    if a.dump:
        dump(a.dump)
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
