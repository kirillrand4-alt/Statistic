"""Persistent, user-scoped working state for the centrifugal sales queue.

The source ``centrifugal.db`` remains read-only.  This module owns the small
``centro_sales.db`` database and deliberately stores assignments, call state,
comments and audit events separately so rebuilding the source cannot erase
sales work.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import bcrypt

ROOT = Path(__file__).resolve().parents[2]
SALES_DB_ENV = "CENTRO_SALES_DB"
DEFAULT_SALES_DB = ROOT / "data" / "centro_sales.db"
ROLES = {"admin", "sales"}
CALL_RESULTS = {"new", "no_answer", "callback", "contacted", "interested",
                "proposal", "not_target", "wrong_number", "completed"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_inn(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))[:12]


def normalize_phone(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return "+" + digits if 10 <= len(digits) <= 15 else ""


def split_values(value: object) -> list[str]:
    seen, result = set(), []
    for item in re.split(r"[|\r\n]+", str(value or "")):
        item = item.strip()
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def sales_db_path() -> Path:
    return Path(os.getenv(SALES_DB_ENV) or DEFAULT_SALES_DB)


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path or sales_db_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
 password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','sales')),
 is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS company_assignment (
 inn TEXT PRIMARY KEY, username TEXT NOT NULL,
 assignment_score REAL NOT NULL DEFAULT 0, assigned_at TEXT NOT NULL,
 source_version TEXT NOT NULL DEFAULT '', assigned_by TEXT NOT NULL DEFAULT 'system',
 FOREIGN KEY(username) REFERENCES users(username) ON UPDATE CASCADE
);
CREATE TABLE IF NOT EXISTS company_state (
 inn TEXT NOT NULL, username TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
 last_contact_at TEXT, next_contact_at TEXT, call_result TEXT NOT NULL DEFAULT 'new',
 updated_at TEXT NOT NULL, PRIMARY KEY(inn, username),
 FOREIGN KEY(username) REFERENCES users(username) ON UPDATE CASCADE
);
CREATE TABLE IF NOT EXISTS company_comment (
 id INTEGER PRIMARY KEY, inn TEXT NOT NULL, username TEXT NOT NULL,
 body TEXT NOT NULL CHECK(length(body) BETWEEN 1 AND 5000),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(username) REFERENCES users(username) ON UPDATE CASCADE
);
CREATE TABLE IF NOT EXISTS activity_log (
 id INTEGER PRIMARY KEY, inn TEXT, username TEXT NOT NULL, action TEXT NOT NULL,
 payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
 FOREIGN KEY(username) REFERENCES users(username) ON UPDATE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_assignment_username ON company_assignment(username, assignment_score DESC);
CREATE INDEX IF NOT EXISTS ix_state_user_status ON company_state(username,status);
CREATE INDEX IF NOT EXISTS ix_state_next ON company_state(username,next_contact_at);
CREATE INDEX IF NOT EXISTS ix_comment_company ON company_comment(inn,username,created_at DESC);
CREATE INDEX IF NOT EXISTS ix_activity_company ON activity_log(inn,created_at DESC);
"""


def init_schema(path: str | Path | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def password_hash(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Пароль должен содержать не менее 10 символов")
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), encoded.encode())
    except (ValueError, TypeError):
        return False


def upsert_user(username: str, password: str, role: str = "sales", *, path=None) -> None:
    username = username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username) or role not in ROLES:
        raise ValueError("Недопустимое имя пользователя или роль")
    now = utcnow()
    with connect(path) as conn:
        conn.execute("INSERT INTO users(username,password_hash,role,created_at,updated_at) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash,"
                     "role=excluded.role,is_active=1,updated_at=excluded.updated_at",
                     (username, password_hash(password), role, now, now))


def authenticate(username: str, password: str, *, path=None) -> dict | None:
    with connect(path) as conn:
        row = conn.execute("SELECT * FROM users WHERE username=? AND is_active=1", (username,)).fetchone()
    return dict(row) if row and verify_password(password, row["password_hash"]) else None


def company_score(c: dict) -> float:
    def num(name, default=0.0):
        try: return float(c.get(name) or default)
        except (TypeError, ValueError): return default
    score = num("moy_prioritet", num("rank_metric"))
    score += .30 * num("vazhnost_pokupki") + .20 * num("dostupnost_kontakta")
    score += 2 * bool(c.get("has_phone") or num("n_phones"))
    score += 2 * bool(c.get("has_purchaser") or num("n_purchaser"))
    score += 2 * bool(c.get("has_tech") or num("n_tech"))
    score += 2 * bool(c.get("has_signal") or num("n_signals"))
    score += min(5, num("faktov_centrobezhnyh", num("n_facts"))) * .2
    status = str(c.get("status_egrul") or c.get("status") or "").casefold()
    if any(x in status for x in ("ликвид", "банкрот", "прекрат")):
        score -= 1000
    return round(score, 4)


def assign_new(companies: list[dict], users=("user1", "user2"), *, path=None,
               source_version="", seed=375) -> dict[str, str]:
    """Persist only previously unseen INNs using deterministic score bands.

    Existing rows are never updated.  Each candidate is greedily given to the
    user with the smallest (count, score, feature counts) balance vector.
    """
    with connect(path) as conn:
        existing = {r[0] for r in conn.execute("SELECT inn FROM company_assignment")}
        totals = {u: [0, 0.0, 0, 0, 0, 0] for u in users}
        for r in conn.execute("SELECT username,COUNT(*),SUM(assignment_score) FROM company_assignment GROUP BY username"):
            if r[0] in totals: totals[r[0]][:2] = [r[1], float(r[2] or 0)]
        unique = {}
        for c in companies:
            inn = normalize_inn(c.get("inn"))
            if inn and inn not in existing: unique.setdefault(inn, c)
        bands: dict[int, list] = {}
        for inn, c in unique.items():
            s = company_score(c)
            bands.setdefault(int(s // 10), []).append((inn, c, s))
        rng = random.Random(seed)
        ordered = []
        for band in sorted(bands, reverse=True):
            chunk = sorted(bands[band], key=lambda x: hashlib.sha256(f"{seed}:{x[0]}".encode()).hexdigest())
            rng.shuffle(chunk)
            ordered.extend(chunk)
        result, now = {}, utcnow()
        for inn, c, score in ordered:
            features = [int(bool(c.get(k))) for k in ("has_phone", "has_purchaser", "has_tech", "has_signal")]
            user = min(users, key=lambda u: (totals[u][0], totals[u][1], *totals[u][2:], u))
            conn.execute("INSERT INTO company_assignment VALUES(?,?,?,?,?,?)",
                         (inn, user, score, now, source_version, "system"))
            totals[user][0] += 1; totals[user][1] += score
            for i, flag in enumerate(features, 2): totals[user][i] += flag
            result[inn] = user
        return result


def allowed(conn: sqlite3.Connection, user: dict, inn: str) -> bool:
    if user["role"] == "admin": return True
    return bool(conn.execute("SELECT 1 FROM company_assignment WHERE inn=? AND username=?",
                             (normalize_inn(inn), user["username"])).fetchone())


def save_call(user: dict, inn: str, result: str, next_contact: str = "", comment: str = "", *, path=None) -> None:
    inn, comment = normalize_inn(inn), comment.strip()
    if result not in CALL_RESULTS or len(comment) > 5000 or (comment == "" and result == "new"):
        raise ValueError("Некорректный результат или комментарий")
    now = utcnow()
    with connect(path) as conn:
        if not allowed(conn, user, inn): raise PermissionError(inn)
        status = "completed" if result in {"completed", "not_target"} else "processed"
        conn.execute("INSERT INTO company_state VALUES(?,?,?,?,?,?,?) ON CONFLICT(inn,username) DO UPDATE SET "
                     "status=excluded.status,last_contact_at=excluded.last_contact_at,next_contact_at=excluded.next_contact_at,"
                     "call_result=excluded.call_result,updated_at=excluded.updated_at",
                     (inn,user["username"],status,now,next_contact or None,result,now))
        if comment:
            conn.execute("INSERT INTO company_comment(inn,username,body,created_at,updated_at) VALUES(?,?,?,?,?)",
                         (inn,user["username"],comment,now,now))
        conn.execute("INSERT INTO activity_log(inn,username,action,payload_json,created_at) VALUES(?,?,?,?,?)",
                     (inn,user["username"],"call_saved",json.dumps({"result":result,"next_contact":next_contact},ensure_ascii=False),now))


def edit_comment(user: dict, comment_id: int, body: str, *, path=None) -> None:
    body = body.strip()
    if not body or len(body) > 5000: raise ValueError("Комментарий должен содержать 1–5000 символов")
    with connect(path) as conn:
        row = conn.execute("SELECT * FROM company_comment WHERE id=?", (comment_id,)).fetchone()
        # Even administrators may inspect both histories, but authorship stays
        # immutable: only the author can edit their own text.
        if not row or row["username"] != user["username"]:
            raise PermissionError(comment_id)
        conn.execute("UPDATE company_comment SET body=?,updated_at=? WHERE id=?", (body,utcnow(),comment_id))
        conn.execute("INSERT INTO activity_log(inn,username,action,payload_json,created_at) VALUES(?,?,?,?,?)",
                     (row["inn"],user["username"],"comment_edited",json.dumps({"id":comment_id}),utcnow()))


def reassign(admin: dict, inn: str, username: str, *, path=None) -> None:
    if admin["role"] != "admin": raise PermissionError("admin")
    with connect(path) as conn:
        if not conn.execute("SELECT 1 FROM users WHERE username=? AND role='sales'",(username,)).fetchone():
            raise ValueError("Неизвестный sales-пользователь")
        conn.execute("UPDATE company_assignment SET username=?,assigned_at=?,assigned_by=? WHERE inn=?",
                     (username,utcnow(),admin["username"],normalize_inn(inn)))
        conn.execute("INSERT INTO activity_log(inn,username,action,payload_json,created_at) VALUES(?,?,?,?,?)",
                     (normalize_inn(inn),admin["username"],"reassigned",json.dumps({"to":username}),utcnow()))
