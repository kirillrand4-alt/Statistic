"""Build ``centrifugal.db`` from the three Centro CSV exports.

Usage (PowerShell)::

    python -m app.tools.build_centro_db `
      --summary C:\data\SVOD375OBEDINENNYY.csv `
      --details C:\data\POLNYY375vsyainformaciya.csv `
      --contacts C:\data\BAZA-CENTROBEZHNIKI-OBSHCHAYA.csv `
      --output C:\seostat\data\centrifugal.db

The output is written to a temporary file and atomically replaces the target
only after all tables and indexes have been created successfully.
"""
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
from typing import Iterable

TECH_RE = re.compile(
    r"тех|инжен|механ|энерг|эксплуатац|ремонт|компресс|главн\w*\s+(?:инжен|механ|энерг)",
    re.I,
)
BUYER_RE = re.compile(r"закуп|снабж|тендер|мто|мтс|материал", re.I)
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

NUMERIC_REAL = {
    "vyruchka_rub",
    "chistaya_pribyl",
    "ball_prioriteta",
    "moy_prioritet",
    "vazhnost_pokupki",
    "dostupnost_kontakta",
}
NUMERIC_INT = {
    "ssch",
    "sostoyaniy",
    "lyudej_v_baze",
    "tehnicheskih",
    "lyudej_iz_faylov",
    "proverok_nadzora",
    "faktov_centrobezhnyh",
    "lyudi_moi_vsego",
    "lyudej_vsego_svedeno",
    "lyudej_s_nomerom",
    "tehnicheskih_s_nomerom",
    "nomerov_bez_vladelca",
}


def clean(value: object) -> str:
    text = str(value or "").strip()
    return "" if text.casefold() in {"nan", "none", "null"} else text


def normalize_inn(value: object) -> str:
    return re.sub(r"\D", "", clean(value))[:12]


def normalize_phone(value: object) -> str:
    digits = re.sub(r"\D", "", clean(value))
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return "+" + digits if 10 <= len(digits) <= 15 else ""


def phones(value: object) -> list[str]:
    text = clean(value)
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[|;,\n]+", text):
        normalized = normalize_phone(chunk)
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(normalized)
    if found:
        return found
    for match in re.findall(r"(?:\+?7|8)?[\s()\-]*\d(?:[\s()\-]*\d){9,10}", text):
        normalized = normalize_phone(match)
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(normalized)
    return found


def emails(value: object) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for match in EMAIL_RE.findall(clean(value)):
        email = match.strip().lower()
        if email not in seen:
            seen.add(email)
            result.append(email)
    return result


def truthy(value: object) -> bool:
    return clean(value).casefold() in {"1", "да", "yes", "true", "y", "on"}


def meaningful_role(value: object) -> bool:
    text = clean(value).casefold()
    return bool(
        text
        and text
        not in {
            "роль не установлена",
            "не установлена",
            "не установлен",
            "общий контакт",
            "владелец неизвестен",
            "нет",
            "—",
        }
    )


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        fieldnames = [clean(name) for name in (reader.fieldnames or [])]
        rows = [{key: clean(value) for key, value in row.items()} for row in reader]
    return fieldnames, rows


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sql_type(name: str) -> str:
    if name in NUMERIC_INT:
        return "INTEGER"
    if name in NUMERIC_REAL:
        return "REAL"
    return "TEXT"


def cast_company(name: str, value: str):
    value = clean(value)
    if not value:
        return None
    try:
        if name in NUMERIC_INT:
            return int(float(value.replace(" ", "").replace(",", ".")))
        if name in NUMERIC_REAL:
            return float(value.replace(" ", "").replace(",", "."))
    except ValueError:
        return None
    return value


def source_url(value: object) -> str:
    url = clean(value)
    if not url:
        return ""
    if url.lower().startswith(("http://", "https://")):
        return url
    return "https://" + url


def add_contact(
    conn: sqlite3.Connection,
    seen: set[tuple],
    *,
    inn: str,
    value: str,
    kind: str,
    person: str = "",
    role: str = "",
    position: str = "",
    source: str = "",
    source_link: str = "",
    phone_type: str = "",
    is_purchaser: bool = False,
    is_tech: bool = False,
    unknown_owner: bool = False,
) -> None:
    value = normalize_phone(value) if kind == "phone" else clean(value).lower()
    if not inn or not value:
        return
    person = clean(person)
    role = clean(role)
    position = clean(position)
    source = clean(source)
    source_link = source_url(source_link)
    has_role = meaningful_role(role) or meaningful_role(position)
    key = (
        inn,
        kind,
        value.casefold(),
        person.casefold(),
        role.casefold(),
        position.casefold(),
        source_link.casefold(),
    )
    if key in seen:
        return
    seen.add(key)
    conn.execute(
        "INSERT INTO contact(inn,value,kind,person,role,position,phone_type,source,source_url,"
        "is_purchaser,is_tech,has_role,is_unknown_owner) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            inn,
            value,
            kind,
            person or None,
            role or None,
            position or None,
            clean(phone_type) or None,
            source or None,
            source_link or None,
            int(is_purchaser),
            int(is_tech),
            int(has_role),
            int(unknown_owner),
        ),
    )


def parse_company_sources(conn: sqlite3.Connection, row: dict[str, str]) -> None:
    inn = normalize_inn(row.get("inn"))
    raw = clean(row.get("istochnik_dopolneniya"))
    for item in re.split(r"[;\n]+", raw):
        item = item.strip()
        if not item:
            continue
        field, sep, src = item.partition("/")
        conn.execute(
            "INSERT INTO company_source(inn,field_name,source,source_url) VALUES(?,?,?,?)",
            (inn, field.strip() or "Сведения о компании", src.strip() if sep else item, None),
        )
    checks = clean(row.get("proverok_nadzora"))
    if checks and checks not in {"0", "0.0"}:
        conn.execute(
            "INSERT INTO company_source(inn,field_name,source,source_url) VALUES(?,?,?,?)",
            (inn, "Проверки надзора", "сводная база проверок", None),
        )


def create_schema(conn: sqlite3.Connection, company_columns: list[str]) -> None:
    invalid = [name for name in company_columns if not IDENT_RE.fullmatch(name)]
    if invalid:
        raise ValueError(f"Недопустимые имена столбцов CSV: {invalid}")
    columns_sql = ",\n".join(f'"{name}" {sql_type(name)}' for name in company_columns)
    conn.executescript(
        f"""
        PRAGMA journal_mode=DELETE;
        PRAGMA foreign_keys=OFF;
        CREATE TABLE company (
          {columns_sql},
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
          id INTEGER PRIMARY KEY,
          inn TEXT NOT NULL,
          value TEXT NOT NULL,
          kind TEXT NOT NULL,
          person TEXT,
          role TEXT,
          position TEXT,
          phone_type TEXT,
          source TEXT,
          source_url TEXT,
          is_purchaser INTEGER NOT NULL DEFAULT 0,
          is_tech INTEGER NOT NULL DEFAULT 0,
          has_role INTEGER NOT NULL DEFAULT 0,
          is_unknown_owner INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE fact (
          id INTEGER PRIMARY KEY,
          inn TEXT NOT NULL,
          status TEXT,
          model TEXT,
          equipment_type TEXT,
          medium TEXT,
          event_date TEXT,
          evidence TEXT,
          quote TEXT,
          source_url TEXT,
          source TEXT
        );
        CREATE TABLE signal (
          id INTEGER PRIMARY KEY,
          inn TEXT NOT NULL,
          title TEXT,
          event_type TEXT,
          event_date TEXT,
          quote TEXT,
          source_url TEXT,
          source TEXT
        );
        CREATE TABLE person (
          id INTEGER PRIMARY KEY,
          inn TEXT NOT NULL,
          person TEXT,
          position TEXT,
          role TEXT,
          phone TEXT,
          phone_type TEXT,
          email TEXT,
          source_url TEXT,
          source TEXT,
          is_tech INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE company_source (
          id INTEGER PRIMARY KEY,
          inn TEXT NOT NULL,
          field_name TEXT,
          source TEXT,
          source_url TEXT
        );
        CREATE TABLE import_info (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
    )


def build(summary: Path, details: Path, contacts_csv: Path, output: Path) -> None:
    for path in (summary, details, contacts_csv):
        if not path.is_file():
            raise FileNotFoundError(path)

    company_columns, summary_rows = read_csv(summary)
    detail_columns, detail_rows = read_csv(details)
    contact_columns, contact_rows = read_csv(contacts_csv)
    if "inn" not in company_columns:
        raise ValueError("В сводном CSV нет столбца inn")
    if "razdel" not in detail_columns:
        raise ValueError("В полном CSV нет столбца razdel")
    if "inn" not in contact_columns:
        raise ValueError("В CSV контактов нет столбца inn")

    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.unlink(missing_ok=True)

    conn = sqlite3.connect(temp)
    seen_contacts: set[tuple] = set()
    try:
        create_schema(conn, company_columns)
        placeholders = ",".join("?" for _ in company_columns)
        quoted = ",".join(f'"{name}"' for name in company_columns)
        for row in summary_rows:
            inn = normalize_inn(row.get("inn"))
            if not inn:
                continue
            row["inn"] = inn
            values = [cast_company(name, row.get(name, "")) for name in company_columns]
            conn.execute(
                f"INSERT OR REPLACE INTO company({quoted}) VALUES({placeholders})",
                values,
            )
            parse_company_sources(conn, row)

            company_name = clean(row.get("predpriyatie"))
            summary_source = "сводная карточка предприятия"
            for field in ("telefony_iz_bazy", "telefony_predpriyatiya", "nomera_bez_vladelca"):
                for phone in phones(row.get(field)):
                    add_contact(
                        conn,
                        seen_contacts,
                        inn=inn,
                        value=phone,
                        kind="phone",
                        person="",
                        role="",
                        source=field,
                        source_link="",
                        unknown_owner=True,
                    )
            for email in emails(row.get("pochta")):
                add_contact(
                    conn,
                    seen_contacts,
                    inn=inn,
                    value=email,
                    kind="email",
                    source=summary_source,
                    unknown_owner=True,
                )

        for row in contact_rows:
            inn = normalize_inn(row.get("inn"))
            if not inn:
                continue
            person_name = clean(row.get("fio"))
            position = clean(row.get("dolzhnost"))
            role = clean(row.get("rol_kanon"))
            role_blob = " ".join((position, role))
            is_purchaser = bool(BUYER_RE.search(role_blob))
            is_tech = truthy(row.get("tehLPR")) or bool(TECH_RE.search(role_blob))
            src = clean(row.get("istochnik"))
            src_link = clean(row.get("ssylka_na_istochnik"))
            phone_values: list[str] = []
            for field in ("mobilnyy_10cifr", "vse_nomera"):
                phone_values.extend(phones(row.get(field)))
            for phone in dict.fromkeys(phone_values):
                add_contact(
                    conn,
                    seen_contacts,
                    inn=inn,
                    value=phone,
                    kind="phone",
                    person=person_name,
                    role=role,
                    position=position,
                    source=src,
                    source_link=src_link,
                    phone_type=clean(row.get("vid_nomera")),
                    is_purchaser=is_purchaser,
                    is_tech=is_tech,
                    unknown_owner=not bool(person_name or meaningful_role(role) or meaningful_role(position)),
                )
            for email in emails(row.get("pochta")):
                add_contact(
                    conn,
                    seen_contacts,
                    inn=inn,
                    value=email,
                    kind="email",
                    person=person_name,
                    role=role,
                    position=position,
                    source=src,
                    source_link=src_link,
                    is_purchaser=is_purchaser,
                    is_tech=is_tech,
                    unknown_owner=not bool(person_name or meaningful_role(role) or meaningful_role(position)),
                )

        for row in detail_rows:
            inn = normalize_inn(row.get("inn"))
            if not inn:
                continue
            section = clean(row.get("razdel")).upper()
            if section == "ФАКТ":
                conn.execute(
                    "INSERT INTO fact(inn,status,model,equipment_type,medium,event_date,evidence,quote,source_url,source) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        inn,
                        clean(row.get("chto")) or None,
                        clean(row.get("kto_ili_marka")) or None,
                        clean(row.get("dolzhnost_ili_tip")) or None,
                        clean(row.get("rol")) or None,
                        clean(row.get("data")) or None,
                        clean(row.get("chem_dokazano")) or None,
                        clean(row.get("citata")) or None,
                        source_url(row.get("ssylka")) or None,
                        clean(row.get("istochnik")) or None,
                    ),
                )
            elif section == "НОВОСТЬ":
                conn.execute(
                    "INSERT INTO signal(inn,title,event_type,event_date,quote,source_url,source) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        inn,
                        clean(row.get("kto_ili_marka")) or None,
                        clean(row.get("chto")) or None,
                        clean(row.get("data")) or None,
                        clean(row.get("citata")) or None,
                        source_url(row.get("ssylka")) or None,
                        clean(row.get("istochnik")) or None,
                    ),
                )
            elif section == "ЧЕЛОВЕК":
                person_name = clean(row.get("kto_ili_marka"))
                position = clean(row.get("dolzhnost_ili_tip"))
                role = clean(row.get("rol"))
                is_tech = truthy(row.get("tehLPR")) or bool(TECH_RE.search(" ".join((position, role))))
                phone_list = phones(row.get("nomer_10cifr"))
                email_list = emails(row.get("pochta"))
                conn.execute(
                    "INSERT INTO person(inn,person,position,role,phone,phone_type,email,source_url,source,is_tech) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        inn,
                        person_name or None,
                        position or None,
                        role or None,
                        phone_list[0] if phone_list else None,
                        clean(row.get("vid_nomera")) or None,
                        email_list[0] if email_list else None,
                        source_url(row.get("ssylka")) or None,
                        clean(row.get("istochnik")) or None,
                        int(is_tech),
                    ),
                )
                is_purchaser = bool(BUYER_RE.search(" ".join((position, role))))
                for phone in phone_list:
                    add_contact(
                        conn,
                        seen_contacts,
                        inn=inn,
                        value=phone,
                        kind="phone",
                        person=person_name,
                        role=role,
                        position=position,
                        source=row.get("istochnik", ""),
                        source_link=row.get("ssylka", ""),
                        phone_type=row.get("vid_nomera", ""),
                        is_purchaser=is_purchaser,
                        is_tech=is_tech,
                    )
                for email in email_list:
                    add_contact(
                        conn,
                        seen_contacts,
                        inn=inn,
                        value=email,
                        kind="email",
                        person=person_name,
                        role=role,
                        position=position,
                        source=row.get("istochnik", ""),
                        source_link=row.get("ssylka", ""),
                        is_purchaser=is_purchaser,
                        is_tech=is_tech,
                    )
            elif section == "НОМЕР":
                for phone in phones(row.get("nomer_10cifr")):
                    add_contact(
                        conn,
                        seen_contacts,
                        inn=inn,
                        value=phone,
                        kind="phone",
                        phone_type=row.get("vid_nomera", ""),
                        source=row.get("istochnik", ""),
                        source_link=row.get("ssylka", ""),
                        unknown_owner=True,
                    )
            elif section == "КАРТОЧКА":
                site = source_url(row.get("ssylka"))
                if site:
                    conn.execute(
                        "INSERT INTO company_source(inn,field_name,source,source_url) VALUES(?,?,?,?)",
                        (inn, "Карточка предприятия", clean(row.get("istochnik")) or "сводная карточка", site),
                    )

        conn.executescript(
            """
            CREATE INDEX ix_contact_inn ON contact(inn, is_purchaser DESC, is_tech DESC, has_role DESC);
            CREATE INDEX ix_fact_inn ON fact(inn, event_date DESC);
            CREATE INDEX ix_fact_model ON fact(model);
            CREATE INDEX ix_signal_inn ON signal(inn);
            CREATE INDEX ix_person_inn ON person(inn, is_tech DESC);
            CREATE INDEX ix_company_source_inn ON company_source(inn);
            """
        )

        conn.execute(
            "UPDATE company SET "
            "n_phones=(SELECT COUNT(*) FROM contact c WHERE c.inn=company.inn AND c.kind='phone'), "
            "n_purchaser=(SELECT COUNT(*) FROM contact c WHERE c.inn=company.inn AND c.is_purchaser=1), "
            "n_tech=(SELECT COUNT(*) FROM contact c WHERE c.inn=company.inn AND c.is_tech=1), "
            "n_signals=(SELECT COUNT(*) FROM signal s WHERE s.inn=company.inn), "
            "n_facts=(SELECT COUNT(*) FROM fact f WHERE f.inn=company.inn)"
        )
        conn.execute(
            "UPDATE company SET has_phone=CASE WHEN n_phones>0 THEN 1 ELSE 0 END, "
            "has_purchaser=CASE WHEN n_purchaser>0 THEN 1 ELSE 0 END, "
            "has_tech=CASE WHEN n_tech>0 THEN 1 ELSE 0 END, "
            "has_signal=CASE WHEN n_signals>0 OR COALESCE(novost,'')<>'' THEN 1 ELSE 0 END"
        )

        text_columns = [name for name in company_columns if sql_type(name) == "TEXT"]
        for row in conn.execute("SELECT inn FROM company").fetchall():
            inn = row[0]
            company = conn.execute(
                f"SELECT {','.join(f'\"{name}\"' for name in text_columns)} FROM company WHERE inn=?",
                (inn,),
            ).fetchone()
            extras = conn.execute(
                "SELECT group_concat(value,' ') FROM contact WHERE inn=?",
                (inn,),
            ).fetchone()[0]
            fact_models = conn.execute(
                "SELECT group_concat(model,' ') FROM fact WHERE inn=?",
                (inn,),
            ).fetchone()[0]
            blob = " ".join(clean(value) for value in (*company, extras, fact_models) if clean(value)).casefold()
            conn.execute("UPDATE company SET search_blob=? WHERE inn=?", (blob, inn))

        info = {
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "company_count": str(conn.execute("SELECT COUNT(*) FROM company").fetchone()[0]),
            "contact_count": str(conn.execute("SELECT COUNT(*) FROM contact").fetchone()[0]),
            "fact_count": str(conn.execute("SELECT COUNT(*) FROM fact").fetchone()[0]),
            "news_count": str(conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0]),
            "person_count": str(conn.execute("SELECT COUNT(*) FROM person").fetchone()[0]),
            "summary_file": summary.name,
            "summary_sha256": sha256(summary),
            "details_file": details.name,
            "details_sha256": sha256(details),
            "contacts_file": contacts_csv.name,
            "contacts_sha256": sha256(contacts_csv),
            "source_kind": "csv-import-v2",
        }
        conn.executemany("INSERT INTO import_info(key,value) VALUES(?,?)", info.items())
        conn.execute("ANALYZE")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    os.replace(temp, output)
    print(f"Готово: {output}")
    print(f"Компаний: {len(summary_rows)}; полных строк: {len(detail_rows)}; контактных строк: {len(contact_rows)}")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Сборка базы Центробежные из CSV")
    value.add_argument("--summary", required=True, type=Path, help="SVOD375OBEDINENNYY.csv")
    value.add_argument("--details", required=True, type=Path, help="POLNYY375vsyainformaciya.csv")
    value.add_argument("--contacts", required=True, type=Path, help="BAZA-CENTROBEZHNIKI-OBSHCHAYA.csv")
    value.add_argument("--output", required=True, type=Path, help="Результирующий centrifugal.db")
    return value


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        build(args.summary, args.details, args.contacts, args.output)
    except Exception as exc:
        print(f"Ошибка сборки: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
