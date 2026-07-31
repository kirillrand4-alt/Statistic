"""Replace Centro equipment facts from SVOD-tri-sostoyaniya.csv.

The company list, contacts, people, news and sales workflow remain unchanged.
Only the ``fact`` table in an already-built centrifugal database is replaced.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from app.services import centro_medium

REQUIRED_FIELDS = {
    "inn", "sostoyanie", "sreda", "marki", "srok_sluzhby",
    "vyvod_ekspertizy", "data", "chto_za_data", "chem_dokazano",
    "tekst", "ssylka", "istochnik",
}


def clean(value: object) -> str:
    text = " ".join(str(value or "").strip().split())
    return "" if text.casefold() in {"nan", "none", "null"} else text


def normalize_inn(value: object) -> str:
    return "".join(character for character in clean(value) if character.isdigit())[:12]


def safe_url(value: object) -> str:
    text = clean(value)
    if not text:
        return ""
    return text if text.lower().startswith(("http://", "https://")) else "https://" + text


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def ensure_fact_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute('PRAGMA table_info("fact")')}
    additions = {
        "commission_conclusion": "TEXT",
        "service_life": "TEXT",
        "date_kind": "TEXT",
    }
    for name, sql_type in additions.items():
        if name not in columns:
            conn.execute(f'ALTER TABLE "fact" ADD COLUMN "{name}" {sql_type}')


def make_fact(row: dict[str, str]) -> dict[str, str]:
    conclusion = clean(row.get("vyvod_ekspertizy"))
    service_life = clean(row.get("srok_sluzhby"))
    date_kind = clean(row.get("chto_za_data"))
    evidence = clean(row.get("chem_dokazano"))

    evidence_parts: list[str] = []
    if conclusion:
        evidence_parts.append(f"Заключение комиссии: {conclusion}")
    if evidence:
        evidence_parts.append(f"Чем доказано: {evidence}")
    if service_life:
        evidence_parts.append(f"Срок службы: {service_life}")
    if date_kind:
        evidence_parts.append(f"Значение даты: {date_kind}")

    return {
        "status": clean(row.get("sostoyanie")),
        "model": clean(row.get("marki")),
        "equipment_type": "",
        "medium": clean(row.get("sreda")),
        "event_date": clean(row.get("data")),
        "evidence": " · ".join(evidence_parts),
        "quote": clean(row.get("tekst")),
        "source_url": safe_url(row.get("ssylka")),
        "source": clean(row.get("istochnik")),
        "commission_conclusion": conclusion,
        "service_life": service_life,
        "date_kind": date_kind,
    }


def fact_key(company_inn: str, fact: dict[str, str]) -> tuple[str, ...]:
    return (
        company_inn,
        fact["status"].casefold(), fact["model"].casefold(),
        fact["medium"].casefold(), fact["event_date"].casefold(),
        fact["evidence"].casefold(), fact["quote"].casefold(),
        fact["source_url"].casefold(),
    )


def rebuild_company_search(conn: sqlite3.Connection) -> None:
    company_columns = conn.execute('PRAGMA table_info("company")').fetchall()
    text_fields = [
        str(row[1]) for row in company_columns
        if str(row[2]).upper() == "TEXT" and str(row[1]) != "search_blob"
    ]
    select_fields = ",".join(f'"{field}"' for field in text_fields)

    for (company_inn,) in conn.execute("SELECT inn FROM company"):
        values = conn.execute(
            f"SELECT {select_fields} FROM company WHERE inn=?", (company_inn,)
        ).fetchone()
        contact_text = conn.execute(
            "SELECT group_concat(value,' ') FROM contact WHERE inn=?", (company_inn,)
        ).fetchone()[0]
        model_text = conn.execute(
            "SELECT group_concat(model,' ') FROM fact WHERE inn=?", (company_inn,)
        ).fetchone()[0]
        blob = " ".join(
            clean(value)
            for value in (company_inn, *values, contact_text, model_text)
            if clean(value)
        ).casefold()
        conn.execute("UPDATE company SET search_blob=? WHERE inn=?", (blob, company_inn))


def replace_database_file(temporary: Path, target: Path) -> None:
    """Replace an existing SQLite file safely on Windows.

    Windows may reject os.replace(temp, target) when the destination already
    exists. Move the original aside first, then install the prepared snapshot.
    Restore the original automatically if the second move fails.
    """
    backup = target.with_suffix(target.suffix + ".facts.bak")
    backup.unlink(missing_ok=True)
    os.replace(target, backup)
    try:
        os.replace(temporary, target)
    except Exception:
        if target.exists():
            target.unlink(missing_ok=True)
        os.replace(backup, target)
        raise
    else:
        backup.unlink(missing_ok=True)


def process(database: Path, facts_csv: Path, *, dry_run: bool = False) -> Counter:
    if not database.is_file():
        raise FileNotFoundError(database)
    if not facts_csv.is_file():
        raise FileNotFoundError(facts_csv)

    target = database
    temporary = database.with_suffix(database.suffix + ".facts.tmp")
    if dry_run:
        connection_path = database
    else:
        temporary.unlink(missing_ok=True)
        shutil.copy2(database, temporary)
        connection_path = temporary

    conn = sqlite3.connect(connection_path)
    counters: Counter = Counter()
    seen: set[tuple[str, ...]] = set()

    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"company", "fact", "contact", "import_info"} <= tables:
            raise ValueError("SQLite не соответствует базе Centro")

        company_inns = {
            normalize_inn(row[0])
            for row in conn.execute("SELECT inn FROM company")
            if normalize_inn(row[0])
        }
        counters["company_count"] = len(company_inns)

        if not dry_run:
            ensure_fact_columns(conn)
            conn.execute("DELETE FROM fact")

        with facts_csv.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=";")
            fields = set(reader.fieldnames or [])
            missing = REQUIRED_FIELDS - fields
            if missing:
                raise ValueError(f"В CSV отсутствуют колонки: {sorted(missing)}")

            for row in reader:
                counters["source_rows"] += 1
                company_inn = normalize_inn(row.get("inn"))
                if company_inn not in company_inns:
                    counters["outside_company_list"] += 1
                    continue

                counters["rows_for_selected_companies"] += 1
                fact = make_fact(row)
                reason = centro_medium.rejection_reason(fact)
                if reason:
                    counters[f"excluded_{reason}"] += 1
                    continue

                if not any(
                    fact[name]
                    for name in (
                        "status", "model", "medium", "event_date", "evidence",
                        "quote", "source_url",
                    )
                ):
                    counters["excluded_empty"] += 1
                    continue

                key = fact_key(company_inn, fact)
                if key in seen:
                    counters["duplicates"] += 1
                    continue
                seen.add(key)

                if fact["commission_conclusion"]:
                    counters["with_commission_conclusion"] += 1

                counters["kept"] += 1
                if dry_run:
                    continue

                conn.execute(
                    """INSERT INTO fact(
                    inn,status,model,equipment_type,medium,event_date,evidence,quote,
                    source_url,source,commission_conclusion,service_life,date_kind
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        company_inn, fact["status"] or None,
                        fact["model"] or None, fact["equipment_type"] or None,
                        fact["medium"] or None, fact["event_date"] or None,
                        fact["evidence"] or None, fact["quote"] or None,
                        fact["source_url"] or None, fact["source"] or None,
                        fact["commission_conclusion"] or None,
                        fact["service_life"] or None, fact["date_kind"] or None,
                    ),
                )

        if not dry_run:
            conn.execute(
                """UPDATE company SET
                n_facts=(SELECT COUNT(*) FROM fact WHERE fact.inn=company.inn)
                """
            )
            rebuild_company_search(conn)

            info = {
                "facts_file": facts_csv.name,
                "facts_sha256": digest(facts_csv),
                "fact_count": str(counters["kept"]),
                "fact_rows_for_selected_companies": str(counters["rows_for_selected_companies"]),
                "excluded_pump_count": str(counters["excluded_pump"]),
                "excluded_non_air_count": str(counters["excluded_non_air"]),
                "duplicate_fact_count": str(counters["duplicates"]),
                "facts_with_commission_conclusion": str(counters["with_commission_conclusion"]),
                "facts_replaced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "facts_source_kind": "SVOD-tri-sostoyaniya-filtered",
            }
            conn.executemany(
                "INSERT OR REPLACE INTO import_info(key,value) VALUES(?,?)",
                info.items(),
            )
            conn.execute("ANALYZE")
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if not dry_run:
        replace_database_file(temporary, target)

    return counters


def print_report(counters: Counter, *, dry_run: bool) -> None:
    print("РЕЖИМ:", "только проверка" if dry_run else "факты заменены")
    labels = (
        ("company_count", "Предприятий в SQLite"),
        ("source_rows", "Всего строк в CSV"),
        ("rows_for_selected_companies", "Строк для выбранных предприятий"),
        ("outside_company_list", "Строк по другим предприятиям"),
        ("excluded_pump", "Исключено насосов"),
        ("excluded_non_air", "Исключено не воздушных сред"),
        ("excluded_empty", "Исключено пустых строк"),
        ("duplicates", "Удалено дублей"),
        ("with_commission_conclusion", "С заключением комиссии"),
        ("kept", "Оставлено фактов"),
    )
    for key, label in labels:
        print(f"{label}: {counters[key]}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Заменить только факты оборудования из SVOD-tri-sostoyaniya.csv"
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--facts", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        counters = process(args.database, args.facts, dry_run=args.dry_run)
        print_report(counters, dry_run=args.dry_run)
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
