"""Read-only access to the replaceable centrifugal-compressor source database.

The sales workflow (users, assignments, call results and comments) lives in
``centro_sales.db``.  This module reads the separate source snapshot built from
CSV files.  Newer snapshots expose detailed facts, news, people and provenance;
older snapshots continue to work with graceful fallbacks.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DB_PATH_ENV = "CENTRIFUGAL_DB"
DEFAULT_DB = ROOT / "data" / "centrifugal.db"
CONNECT_TIMEOUT = 5.0


class CentroDbUnavailable(RuntimeError):
    pass


def db_path() -> Path:
    return Path((os.getenv(DB_PATH_ENV) or "").strip() or DEFAULT_DB)


def connect() -> sqlite3.Connection:
    path = db_path()
    if not path.exists():
        raise CentroDbUnavailable(
            f"База центробежных не найдена: {path}. "
            f"Укажите {DB_PATH_ENV} или соберите новый centrifugal.db."
        )
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=CONNECT_TIMEOUT)
    except sqlite3.Error as exc:
        raise CentroDbUnavailable(f"Не удалось открыть {path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(CONNECT_TIMEOUT * 1000)}")
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    if not table_exists(conn, name):
        return set()
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{name}")')}


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


def source_version() -> str:
    try:
        stat = db_path().stat()
        return f"{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        return "unknown"


def database_info() -> dict[str, str]:
    with connect() as conn:
        if not table_exists(conn, "import_info"):
            return {
                "database_path": str(db_path()),
                "source_version": source_version(),
                "source_kind": "legacy",
            }
        rows = conn.execute("SELECT key,value FROM import_info ORDER BY key").fetchall()
    info = {str(row[0]): str(row[1] or "") for row in rows}
    info.setdefault("database_path", str(db_path()))
    info.setdefault("source_version", source_version())
    info.setdefault("source_kind", "csv-import")
    return info


def list_companies() -> list[dict]:
    with connect() as conn:
        if not table_exists(conn, "company"):
            raise CentroDbUnavailable("В базе нет таблицы company")
        return _rows(conn, "SELECT * FROM company")


_URL_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)


def safe_source_url(raw: object) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value.lower().startswith(("http://", "https://")):
        return value
    if value.startswith("//"):
        return "https:" + value
    if _URL_SCHEME.match(value):
        return ""
    return "https://" + value


def normalize_phone(raw: object) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return "+" + digits if 10 <= len(digits) <= 15 else ""


def contact_href(kind: str, value: str) -> str:
    if kind == "phone":
        normalized = normalize_phone(value)
        return "tel:" + normalized if normalized else ""
    if kind == "email":
        return "mailto:" + value.strip()
    return ""


def _meaningful_role(value: object) -> bool:
    text = str(value or "").strip().casefold()
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


def contacts(inn: str) -> list[dict]:
    with connect() as conn:
        columns = table_columns(conn, "contact")
        if not columns:
            return []
        order_parts: list[str] = []
        if "is_purchaser" in columns:
            order_parts.append("COALESCE(is_purchaser,0) DESC")
        if "is_tech" in columns:
            order_parts.append("COALESCE(is_tech,0) DESC")
        if "has_role" in columns:
            order_parts.append("COALESCE(has_role,0) DESC")
        if "kind" in columns:
            order_parts.append("CASE kind WHEN 'phone' THEN 0 WHEN 'email' THEN 1 ELSE 2 END")
        order_parts.append("id")
        rows = _rows(
            conn,
            f"SELECT * FROM contact WHERE inn=? ORDER BY {', '.join(order_parts)}",
            (inn,),
        )

    result: list[dict] = []
    for row in rows:
        item = dict(row)
        kind = str(item.get("kind") or "").strip().lower()
        value = str(item.get("value") or "").strip()
        if kind == "phone":
            value = normalize_phone(value) or value
        item["kind"] = kind
        item["value"] = value
        item["person"] = str(item.get("person") or "").strip()
        item["role"] = str(item.get("role") or item.get("position") or "").strip()
        item["source"] = str(item.get("source") or "").strip()
        item["source_url"] = safe_source_url(item.get("source_url"))
        item["href"] = contact_href(kind, value)
        item["is_purchaser"] = int(bool(item.get("is_purchaser")))
        item["is_tech"] = int(bool(item.get("is_tech")))
        item["is_unknown_owner"] = int(bool(item.get("is_unknown_owner")))
        item["has_role"] = int(
            bool(item.get("has_role"))
            or item["is_purchaser"]
            or item["is_tech"]
            or _meaningful_role(item["role"])
        )
        result.append(item)
    return result


def facts(inn: str, limit: int = 500) -> list[dict]:
    with connect() as conn:
        columns = table_columns(conn, "fact")
        if not columns:
            return []
        date_col = "event_date" if "event_date" in columns else "data" if "data" in columns else ""
        order = f"CASE WHEN COALESCE({date_col},'')='' THEN 1 ELSE 0 END, {date_col} DESC, id DESC" if date_col else "id DESC"
        rows = _rows(
            conn,
            f"SELECT * FROM fact WHERE inn=? ORDER BY {order} LIMIT ?",
            (inn, max(1, min(limit, 2000))),
        )
    result: list[dict] = []
    for row in rows:
        result.append(
            {
                **row,
                "status": row.get("status") or row.get("chto") or "",
                "model": row.get("model") or row.get("kto_ili_marka") or "",
                "equipment_type": row.get("equipment_type") or row.get("dolzhnost_ili_tip") or "",
                "medium": row.get("medium") or row.get("rol") or "",
                "event_date": row.get("event_date") or row.get("data") or "",
                "evidence": row.get("evidence") or row.get("chem_dokazano") or "",
                "quote": row.get("quote") or row.get("citata") or "",
                "source": row.get("source") or row.get("istochnik") or "",
                "source_url": safe_source_url(row.get("source_url") or row.get("ssylka")),
            }
        )
    return result


def signals(inn: str) -> list[dict]:
    with connect() as conn:
        columns = table_columns(conn, "signal")
        if not columns:
            return []
        rows = _rows(conn, "SELECT * FROM signal WHERE inn=? ORDER BY id DESC", (inn,))
    result: list[dict] = []
    for row in rows:
        result.append(
            {
                **row,
                "title": row.get("title") or row.get("what") or row.get("kto_ili_marka") or "Новостной повод",
                "event_type": row.get("event_type") or row.get("chto") or "",
                "event_date": row.get("event_date") or row.get("ts") or row.get("data") or "",
                "quote": row.get("quote") or row.get("citata") or "",
                "source": row.get("source") or row.get("istochnik") or "",
                "source_url": safe_source_url(row.get("source_url") or row.get("ssylka")),
            }
        )
    return result


def persons(inn: str) -> list[dict]:
    with connect() as conn:
        columns = table_columns(conn, "person")
        if not columns:
            return []
        rows = _rows(
            conn,
            "SELECT * FROM person WHERE inn=? "
            "ORDER BY COALESCE(is_tech,0) DESC, CASE WHEN COALESCE(role,'')<>'' THEN 0 ELSE 1 END, id",
            (inn,),
        )
    result: list[dict] = []
    for row in rows:
        role = str(row.get("role") or "").strip()
        position = str(row.get("position") or row.get("dolzhnost") or "").strip()
        result.append(
            {
                **row,
                "person": row.get("person") or row.get("fio") or "",
                "position": position,
                "role": role,
                "phone": normalize_phone(row.get("phone") or row.get("nomer_10cifr")),
                "email": row.get("email") or row.get("pochta") or "",
                "source": row.get("source") or row.get("istochnik") or "",
                "source_url": safe_source_url(row.get("source_url") or row.get("ssylka")),
                "is_tech": int(bool(row.get("is_tech") or str(row.get("tehLPR") or "").casefold() == "да")),
                "has_role": int(_meaningful_role(role) or _meaningful_role(position)),
            }
        )
    return result


def company_sources(inn: str) -> list[dict]:
    with connect() as conn:
        columns = table_columns(conn, "company_source")
        if not columns:
            return []
        rows = _rows(conn, "SELECT * FROM company_source WHERE inn=? ORDER BY id", (inn,))
    return [
        {
            **row,
            "field_name": row.get("field_name") or row.get("field") or "Сведения о компании",
            "source": row.get("source") or "",
            "source_url": safe_source_url(row.get("source_url")),
        }
        for row in rows
    ]
