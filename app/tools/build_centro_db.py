"""Build centrifugal.db from the three Centro CSV exports."""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.services import centro_medium

TECH_RE = re.compile(r"тех|инжен|механ|энерг|эксплуатац|ремонт|компресс", re.I)
BUY_RE = re.compile(r"закуп|снабж|тендер|мто|мтс|материал", re.I)
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REAL_FIELDS = {
    "vyruchka_rub", "chistaya_pribyl", "ball_prioriteta",
    "moy_prioritet", "vazhnost_pokupki", "dostupnost_kontakta",
}
INT_FIELDS = {
    "ssch", "sostoyaniy", "lyudej_v_baze", "tehnicheskih",
    "lyudej_iz_faylov", "proverok_nadzora", "faktov_centrobezhnyh",
    "lyudi_moi_vsego", "lyudej_vsego_svedeno", "lyudej_s_nomerom",
    "tehnicheskih_s_nomerom", "nomerov_bez_vladelca",
}


def clean(value: object) -> str:
    text = str(value or "").strip()
    return "" if text.casefold() in {"nan", "none", "null"} else text


def inn(value: object) -> str:
    return re.sub(r"\D", "", clean(value))[:12]


def phone(value: object) -> str:
    digits = re.sub(r"\D", "", clean(value))
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return "+" + digits if 10 <= len(digits) <= 15 else ""


def phone_list(value: object) -> list[str]:
    text = clean(value)
    result: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[|;,\n]+", text):
        normalized = phone(item)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def email_list(value: object) -> list[str]:
    return list(dict.fromkeys(match.lower() for match in EMAIL_RE.findall(clean(value))))


def truthy(value: object) -> bool:
    return clean(value).casefold() in {"1", "да", "yes", "true", "y", "on"}


def has_role(value: object) -> bool:
    text = clean(value).casefold()
    return bool(text and text not in {
        "роль не установлена", "не установлена", "не установлен",
        "общий контакт", "владелец неизвестен", "нет", "—",
    })


def url(value: object) -> str:
    text = clean(value)
    if not text:
        return ""
    return text if text.lower().startswith(("http://", "https://")) else "https://" + text


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        fields = [clean(field) for field in (reader.fieldnames or [])]
        rows = [{str(key): clean(value) for key, value in row.items()} for row in reader]
    return fields, rows


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def sql_type(field: str) -> str:
    if field in INT_FIELDS:
        return "INTEGER"
    if field in REAL_FIELDS:
        return "REAL"
    return "TEXT"


def cast(field: str, value: object):
    text = clean(value)
    if not text:
        return None
    try:
        normalized = text.replace(" ", "").replace(",", ".")
        if field in INT_FIELDS:
            return int(float(normalized))
        if field in REAL_FIELDS:
            return float(normalized)
    except ValueError:
        return None
    return text


def create_schema(conn: sqlite3.Connection, fields: list[str]) -> None:
    bad = [field for field in fields if not IDENT_RE.fullmatch(field)]
    if bad:
        raise ValueError(f"Недопустимые имена столбцов: {bad}")
    original = ",\n".join(f'"{field}" {sql_type(field)}' for field in fields)
    conn.executescript(f"""
    PRAGMA journal_mode=DELETE;
    CREATE TABLE company (
      {original},
      has_phone INTEGER NOT NULL DEFAULT 0,
      has_purchaser INTEGER NOT NULL DEFAULT 0,
      has_tech INTEGER NOT NULL DEFAULT 0,
      has_signal INTEGER NOT NULL DEFAULT 0,
      n_phones INTEGER NOT NULL DEFAULT 0,
      n_purchaser INTEGER NOT NULL DEFAULT 0,
      n_tech INTEGER NOT NULL DEFAULT 0,
      n_signals INTEGER NOT NULL DEFAULT 0,
      n_facts INTEGER NOT NULL DEFAULT 0,
      search_blob TEXT NOT NULL DEFAULT '',
      PRIMARY KEY(inn)
    );
    CREATE TABLE contact (
      id INTEGER PRIMARY KEY, inn TEXT NOT NULL, value TEXT NOT NULL, kind TEXT NOT NULL,
      person TEXT, role TEXT, position TEXT, phone_type TEXT, source TEXT, source_url TEXT,
      is_purchaser INTEGER NOT NULL DEFAULT 0, is_tech INTEGER NOT NULL DEFAULT 0,
      has_role INTEGER NOT NULL DEFAULT 0, is_unknown_owner INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE fact (
      id INTEGER PRIMARY KEY, inn TEXT NOT NULL, status TEXT, model TEXT,
      equipment_type TEXT, medium TEXT, event_date TEXT, evidence TEXT,
      quote TEXT, source_url TEXT, source TEXT
    );
    CREATE TABLE signal (
      id INTEGER PRIMARY KEY, inn TEXT NOT NULL, title TEXT, event_type TEXT,
      event_date TEXT, quote TEXT, source_url TEXT, source TEXT
    );
    CREATE TABLE person (
      id INTEGER PRIMARY KEY, inn TEXT NOT NULL, person TEXT, position TEXT, role TEXT,
      phone TEXT, phone_type TEXT, email TEXT, source_url TEXT, source TEXT,
      is_tech INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE company_source (
      id INTEGER PRIMARY KEY, inn TEXT NOT NULL, field_name TEXT, source TEXT, source_url TEXT
    );
    CREATE TABLE import_info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)


def add_contact(conn: sqlite3.Connection, seen: set[tuple], *, company_inn: str,
                value: str, kind: str, person_name: str = "", role: str = "",
                position: str = "", phone_type: str = "", source: str = "",
                source_url: str = "", purchaser: bool = False, tech: bool = False,
                unknown: bool = False) -> None:
    normalized = phone(value) if kind == "phone" else clean(value).lower()
    if not company_inn or not normalized:
        return
    key = (
        company_inn, kind, normalized.casefold(), clean(person_name).casefold(),
        clean(role).casefold(), clean(position).casefold(), url(source_url).casefold(),
    )
    if key in seen:
        return
    seen.add(key)
    role_known = has_role(role) or has_role(position)
    conn.execute(
        """INSERT INTO contact(
        inn,value,kind,person,role,position,phone_type,source,source_url,
        is_purchaser,is_tech,has_role,is_unknown_owner
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            company_inn, normalized, kind, clean(person_name) or None,
            clean(role) or None, clean(position) or None, clean(phone_type) or None,
            clean(source) or None, url(source_url) or None, int(purchaser), int(tech),
            int(role_known), int(unknown),
        ),
    )


def add_summary_sources(conn: sqlite3.Connection, row: dict[str, str]) -> None:
    company_inn = inn(row.get("inn"))
    for item in re.split(r"[;\n]+", clean(row.get("istochnik_dopolneniya"))):
        item = item.strip()
        if not item:
            continue
        field, sep, source = item.partition("/")
        conn.execute(
            "INSERT INTO company_source(inn,field_name,source,source_url) VALUES(?,?,?,?)",
            (company_inn, field.strip() or "Сведения о компании",
             source.strip() if sep else item, None),
        )
    checks = clean(row.get("proverok_nadzora"))
    if checks and checks not in {"0", "0.0"}:
        links = re.split(r"\s*\|\s*", clean(row.get("ssylki_na_istochniki")))
        usable = next((url(item) for item in links if "monitor-pb" in item), "")
        conn.execute(
            "INSERT INTO company_source(inn,field_name,source,source_url) VALUES(?,?,?,?)",
            (company_inn, f"Проверки надзора: {checks}", "реестр проверок", usable or None),
        )


def build(summary: Path, details: Path, contacts: Path, output: Path) -> None:
    for path in (summary, details, contacts):
        if not path.is_file():
            raise FileNotFoundError(path)

    company_fields, company_rows = read_csv(summary)
    detail_fields, detail_rows = read_csv(details)
    contact_fields, contact_rows = read_csv(contacts)
    if "inn" not in company_fields or "razdel" not in detail_fields or "inn" not in contact_fields:
        raise ValueError("CSV не соответствует ожидаемой структуре")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    conn = sqlite3.connect(temporary)
    seen: set[tuple] = set()
    excluded_facts = 0
    try:
        create_schema(conn, company_fields)
        quoted = ",".join(f'"{field}"' for field in company_fields)
        placeholders = ",".join("?" for _ in company_fields)

        for row in company_rows:
            company_inn = inn(row.get("inn"))
            if not company_inn:
                continue
            row["inn"] = company_inn
            conn.execute(
                f"INSERT OR REPLACE INTO company({quoted}) VALUES({placeholders})",
                [cast(field, row.get(field)) for field in company_fields],
            )
            add_summary_sources(conn, row)
            for field in ("telefony_iz_bazy", "telefony_predpriyatiya", "nomera_bez_vladelca"):
                for value in phone_list(row.get(field)):
                    add_contact(
                        conn, seen, company_inn=company_inn, value=value, kind="phone",
                        source=field, unknown=True,
                    )
            for value in email_list(row.get("pochta")):
                add_contact(
                    conn, seen, company_inn=company_inn, value=value, kind="email",
                    source="сводная карточка предприятия", unknown=True,
                )

        for row in contact_rows:
            company_inn = inn(row.get("inn"))
            if not company_inn:
                continue
            person_name = clean(row.get("fio"))
            position = clean(row.get("dolzhnost"))
            role = clean(row.get("rol_kanon"))
            role_blob = " ".join((position, role))
            purchaser = bool(BUY_RE.search(role_blob))
            tech = truthy(row.get("tehLPR")) or bool(TECH_RE.search(role_blob))
            unknown = not bool(person_name or has_role(role) or has_role(position))
            values: list[str] = []
            for field in ("mobilnyy_10cifr", "vse_nomera"):
                values.extend(phone_list(row.get(field)))
            for value in dict.fromkeys(values):
                add_contact(
                    conn, seen, company_inn=company_inn, value=value, kind="phone",
                    person_name=person_name, role=role, position=position,
                    phone_type=row.get("vid_nomera", ""), source=row.get("istochnik", ""),
                    source_url=row.get("ssylka_na_istochnik", ""), purchaser=purchaser,
                    tech=tech, unknown=unknown,
                )
            for value in email_list(row.get("pochta")):
                add_contact(
                    conn, seen, company_inn=company_inn, value=value, kind="email",
                    person_name=person_name, role=role, position=position,
                    source=row.get("istochnik", ""), source_url=row.get("ssylka_na_istochnik", ""),
                    purchaser=purchaser, tech=tech, unknown=unknown,
                )

        for row in detail_rows:
            company_inn = inn(row.get("inn"))
            if not company_inn:
                continue
            section = clean(row.get("razdel")).upper()
            if section == "ФАКТ":
                fact = {
                    "status": clean(row.get("chto")),
                    "model": clean(row.get("kto_ili_marka")),
                    "equipment_type": clean(row.get("dolzhnost_ili_tip")),
                    "medium": clean(row.get("rol")),
                    "event_date": clean(row.get("data")),
                    "evidence": clean(row.get("chem_dokazano")),
                    "quote": clean(row.get("citata")),
                    "source_url": url(row.get("ssylka")),
                    "source": clean(row.get("istochnik")),
                }
                if not centro_medium.keep_fact(fact):
                    excluded_facts += 1
                    continue
                conn.execute(
                    """INSERT INTO fact(
                    inn,status,model,equipment_type,medium,event_date,evidence,quote,source_url,source
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        company_inn, fact["status"] or None, fact["model"] or None,
                        fact["equipment_type"] or None, fact["medium"] or None,
                        fact["event_date"] or None, fact["evidence"] or None,
                        fact["quote"] or None, fact["source_url"] or None,
                        fact["source"] or None,
                    ),
                )
            elif section == "НОВОСТЬ":
                conn.execute(
                    """INSERT INTO signal(
                    inn,title,event_type,event_date,quote,source_url,source
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        company_inn, clean(row.get("kto_ili_marka")) or None,
                        clean(row.get("chto")) or None, clean(row.get("data")) or None,
                        clean(row.get("citata")) or None, url(row.get("ssylka")) or None,
                        clean(row.get("istochnik")) or None,
                    ),
                )
            elif section == "ЧЕЛОВЕК":
                person_name = clean(row.get("kto_ili_marka"))
                position = clean(row.get("dolzhnost_ili_tip"))
                role = clean(row.get("rol"))
                tech = truthy(row.get("tehLPR")) or bool(TECH_RE.search(" ".join((position, role))))
                numbers = phone_list(row.get("nomer_10cifr"))
                addresses = email_list(row.get("pochta"))
                conn.execute(
                    """INSERT INTO person(
                    inn,person,position,role,phone,phone_type,email,source_url,source,is_tech
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        company_inn, person_name or None, position or None, role or None,
                        numbers[0] if numbers else None, clean(row.get("vid_nomera")) or None,
                        addresses[0] if addresses else None, url(row.get("ssylka")) or None,
                        clean(row.get("istochnik")) or None, int(tech),
                    ),
                )
                purchaser = bool(BUY_RE.search(" ".join((position, role))))
                for value in numbers:
                    add_contact(
                        conn, seen, company_inn=company_inn, value=value, kind="phone",
                        person_name=person_name, role=role, position=position,
                        phone_type=row.get("vid_nomera", ""), source=row.get("istochnik", ""),
                        source_url=row.get("ssylka", ""), purchaser=purchaser, tech=tech,
                    )
                for value in addresses:
                    add_contact(
                        conn, seen, company_inn=company_inn, value=value, kind="email",
                        person_name=person_name, role=role, position=position,
                        source=row.get("istochnik", ""), source_url=row.get("ssylka", ""),
                        purchaser=purchaser, tech=tech,
                    )
            elif section == "НОМЕР":
                for value in phone_list(row.get("nomer_10cifr")):
                    add_contact(
                        conn, seen, company_inn=company_inn, value=value, kind="phone",
                        phone_type=row.get("vid_nomera", ""), source=row.get("istochnik", ""),
                        source_url=row.get("ssylka", ""), unknown=True,
                    )
            elif section == "КАРТОЧКА" and clean(row.get("ssylka")):
                conn.execute(
                    "INSERT INTO company_source(inn,field_name,source,source_url) VALUES(?,?,?,?)",
                    (
                        company_inn, "Карточка предприятия",
                        clean(row.get("istochnik")) or "карточка предприятия",
                        url(row.get("ssylka")),
                    ),
                )

        conn.executescript("""
        CREATE INDEX ix_contact_inn ON contact(inn,is_purchaser DESC,is_tech DESC,has_role DESC);
        CREATE INDEX ix_fact_inn ON fact(inn,event_date DESC);
        CREATE INDEX ix_fact_model ON fact(model);
        CREATE INDEX ix_signal_inn ON signal(inn,event_date DESC);
        CREATE INDEX ix_person_inn ON person(inn,is_tech DESC);
        CREATE INDEX ix_source_inn ON company_source(inn);
        """)

        conn.execute("""
        UPDATE company SET
          n_phones=(SELECT COUNT(*) FROM contact c WHERE c.inn=company.inn AND c.kind='phone'),
          n_purchaser=(SELECT COUNT(*) FROM contact c WHERE c.inn=company.inn AND c.is_purchaser=1),
          n_tech=(SELECT COUNT(*) FROM contact c WHERE c.inn=company.inn AND c.is_tech=1),
          n_signals=(SELECT COUNT(*) FROM signal s WHERE s.inn=company.inn),
          n_facts=(SELECT COUNT(*) FROM fact f WHERE f.inn=company.inn)
        """)
        conn.execute("""
        UPDATE company SET
          has_phone=CASE WHEN n_phones>0 THEN 1 ELSE 0 END,
          has_purchaser=CASE WHEN n_purchaser>0 THEN 1 ELSE 0 END,
          has_tech=CASE WHEN n_tech>0 THEN 1 ELSE 0 END,
          has_signal=CASE WHEN n_signals>0 OR COALESCE(novost,'')<>'' THEN 1 ELSE 0 END
        """)

        text_fields = [field for field in company_fields if sql_type(field) == "TEXT"]
        select_fields = ",".join(f'"{field}"' for field in text_fields)
        for row in conn.execute("SELECT inn FROM company"):
            company_inn = row[0]
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
                clean(value) for value in (company_inn, *values, contact_text, model_text)
                if clean(value)
            ).casefold()
            conn.execute("UPDATE company SET search_blob=? WHERE inn=?", (blob, company_inn))

        info = {
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "company_count": str(conn.execute("SELECT COUNT(*) FROM company").fetchone()[0]),
            "contact_count": str(conn.execute("SELECT COUNT(*) FROM contact").fetchone()[0]),
            "fact_count": str(conn.execute("SELECT COUNT(*) FROM fact").fetchone()[0]),
            "excluded_fact_count": str(excluded_facts),
            "news_count": str(conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0]),
            "person_count": str(conn.execute("SELECT COUNT(*) FROM person").fetchone()[0]),
            "summary_file": summary.name, "summary_sha256": digest(summary),
            "details_file": details.name, "details_sha256": digest(details),
            "contacts_file": contacts.name, "contacts_sha256": digest(contacts),
            "source_kind": "csv-import-v3-air-compressors",
        }
        conn.executemany("INSERT INTO import_info(key,value) VALUES(?,?)", info.items())
        conn.execute("ANALYZE")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    os.replace(temporary, output)
    print(f"Готово: {output}")
    print(
        f"Компаний: {len(company_rows)}; полных строк: {len(detail_rows)}; "
        f"контактных строк: {len(contact_rows)}; исключено фактов: {excluded_facts}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Сборка базы Центробежные из CSV")
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--details", required=True, type=Path)
    parser.add_argument("--contacts", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        build(args.summary, args.details, args.contacts, args.output)
    except Exception as exc:
        print(f"Ошибка сборки: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
