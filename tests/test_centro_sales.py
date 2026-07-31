import sqlite3

import pytest

from app.services import centro_sales as sales


@pytest.fixture()
def sales_db(tmp_path, monkeypatch):
    path = tmp_path / "centro_sales.db"
    monkeypatch.setenv("CENTRO_SALES_DB", str(path))
    sales.init_schema(path)
    sales.upsert_user("admin", "very-safe-admin", "admin", path=path)
    sales.upsert_user("user1", "very-safe-user1", "sales", path=path)
    sales.upsert_user("user2", "very-safe-user2", "sales", path=path)
    return path


def companies(count=10):
    return [
        {
            "inn": f"7700000{index:03d}",
            "moy_prioritet": 100 - index,
            "has_phone": index % 2 == 0,
            "has_purchaser": index % 3 == 0,
            "has_tech": index % 4 == 0,
            "has_signal": index % 5 == 0,
        }
        for index in range(count)
    ]


def user(path, name):
    with sales.connect(path) as conn:
        return dict(
            conn.execute(
                "SELECT * FROM users WHERE username=?", (name,)
            ).fetchone()
        )


def test_assignment_is_balanced_and_persistent(sales_db):
    first = sales.assign_new(companies(), path=sales_db)
    assert len(first) == 10
    with sales.connect(sales_db) as conn:
        counts = dict(
            conn.execute(
                "SELECT username,COUNT(*) FROM company_assignment GROUP BY username"
            )
        )
        original = dict(
            conn.execute("SELECT inn,username FROM company_assignment")
        )
    assert abs(counts["user1"] - counts["user2"]) <= 1
    assert sales.assign_new(list(reversed(companies())), path=sales_db) == {}
    with sales.connect(sales_db) as conn:
        assert dict(conn.execute("SELECT inn,username FROM company_assignment")) == original


def test_only_new_companies_are_assigned(sales_db):
    sales.assign_new(companies(8), path=sales_db)
    with sales.connect(sales_db) as conn:
        before = dict(conn.execute("SELECT inn,username FROM company_assignment"))
    assigned = sales.assign_new(companies(11), path=sales_db)
    assert len(assigned) == 3
    with sales.connect(sales_db) as conn:
        after = dict(conn.execute("SELECT inn,username FROM company_assignment"))
        counts = dict(
            conn.execute(
                "SELECT username,COUNT(*) FROM company_assignment GROUP BY username"
            )
        )
    assert all(after[inn] == owner for inn, owner in before.items())
    assert abs(counts["user1"] - counts["user2"]) <= 1


def test_only_active_sales_users_receive_new_assignments(sales_db):
    with sales.connect(sales_db) as conn:
        conn.execute("UPDATE users SET is_active=0 WHERE username='user2'")
    assigned = sales.assign_new(companies(4), users=("user1", "user2"), path=sales_db)
    assert assigned
    assert set(assigned.values()) == {"user1"}


def test_user_cannot_access_other_assignment(sales_db):
    sales.assign_new(companies(2), path=sales_db)
    with sales.connect(sales_db) as conn:
        assignments = dict(
            conn.execute("SELECT inn,username FROM company_assignment")
        )
        for inn, owner in assignments.items():
            assert sales.allowed(conn, user(sales_db, owner), inn)
            other = "user2" if owner == "user1" else "user1"
            assert not sales.allowed(conn, user(sales_db, other), inn)
            assert sales.allowed(conn, user(sales_db, "admin"), inn)


def test_comments_persist_and_cannot_be_edited_by_other_user(sales_db):
    sales.assign_new(companies(2), path=sales_db)
    with sales.connect(sales_db) as conn:
        inn, owner = conn.execute(
            "SELECT inn,username FROM company_assignment LIMIT 1"
        ).fetchone()
    owner_user = user(sales_db, owner)
    sales.save_call(
        owner_user,
        inn,
        "interested",
        comment="Перезвонить после обеда",
        path=sales_db,
    )
    with sales.connect(sales_db) as conn:
        comment = conn.execute(
            "SELECT * FROM company_comment WHERE inn=?", (inn,)
        ).fetchone()
        assert comment["body"] == "Перезвонить после обеда"
        assert (
            conn.execute(
                "SELECT call_result FROM company_state WHERE inn=?", (inn,)
            ).fetchone()[0]
            == "interested"
        )
    other = user(sales_db, "user2" if owner == "user1" else "user1")
    with pytest.raises(PermissionError):
        sales.edit_comment(other, comment["id"], "Чужая правка", path=sales_db)
    with pytest.raises(PermissionError):
        sales.edit_comment(
            user(sales_db, "admin"),
            comment["id"],
            "Правка администратора",
            path=sales_db,
        )


def test_admin_updates_assigned_users_state(sales_db):
    sales.assign_new(companies(1), path=sales_db)
    with sales.connect(sales_db) as conn:
        inn, owner = conn.execute(
            "SELECT inn,username FROM company_assignment"
        ).fetchone()
    sales.save_call(
        user(sales_db, "admin"),
        inn,
        "proposal",
        comment="Администратор уточнил статус",
        path=sales_db,
    )
    with sales.connect(sales_db) as conn:
        state = conn.execute(
            "SELECT username,call_result FROM company_state WHERE inn=?", (inn,)
        ).fetchone()
        assert tuple(state) == (owner, "proposal")
        comment = conn.execute(
            "SELECT username FROM company_comment WHERE inn=?", (inn,)
        ).fetchone()
        assert comment[0] == "admin"


def test_reassignment_preserves_call_state(sales_db):
    sales.assign_new(companies(1), path=sales_db)
    with sales.connect(sales_db) as conn:
        inn, old_owner = conn.execute(
            "SELECT inn,username FROM company_assignment"
        ).fetchone()
    new_owner = "user2" if old_owner == "user1" else "user1"
    sales.save_call(
        user(sales_db, old_owner),
        inn,
        "callback",
        next_contact="2026-08-01T10:30",
        comment="Перезвонить завтра",
        path=sales_db,
    )
    sales.reassign(
        user(sales_db, "admin"),
        inn,
        new_owner,
        path=sales_db,
    )
    with sales.connect(sales_db) as conn:
        assert sales.assignment_owner(conn, inn) == new_owner
        state = conn.execute(
            "SELECT call_result,next_contact_at FROM company_state "
            "WHERE inn=? AND username=?",
            (inn, new_owner),
        ).fetchone()
        assert tuple(state) == ("callback", "2026-08-01T10:30")
        event = conn.execute(
            "SELECT payload_json FROM activity_log "
            "WHERE action='reassigned' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        assert old_owner in event and new_owner in event


def test_schema_migrates_old_assignment_table(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
              id INTEGER PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT,
              role TEXT, is_active INTEGER, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE company_assignment (
              inn TEXT PRIMARY KEY, username TEXT, assignment_score REAL,
              assigned_at TEXT, source_version TEXT, assigned_by TEXT
            );
            """
        )
    sales.init_schema(path)
    with sales.connect(path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(company_assignment)")
        }
    assert {"has_phone", "has_purchaser", "has_tech", "has_signal"} <= columns


def test_phone_and_multivalue_normalization():
    assert sales.normalize_phone("8 (999) 123-45-67") == "+79991234567"
    assert sales.normalize_phone("+7 999 123 45 67") == "+79991234567"
    assert sales.split_values("ABB | Wilo\nABB; Atlas Copco") == [
        "ABB",
        "Wilo",
        "Atlas Copco",
    ]


def test_source_database_is_not_modified(tmp_path):
    source = tmp_path / "centrifugal.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE company(inn TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO company VALUES('7700000000')")
    digest = source.read_bytes()
    sales_path = tmp_path / "sales.db"
    sales.init_schema(sales_path)
    assert source.read_bytes() == digest
