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
    return [{"inn": f"7700000{i:03d}", "moy_prioritet": 100-i,
             "has_phone": i % 2 == 0, "has_purchaser": i % 3 == 0,
             "has_tech": i % 4 == 0, "has_signal": i % 5 == 0} for i in range(count)]


def user(path, name):
    with sales.connect(path) as conn:
        return dict(conn.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone())


def test_assignment_is_balanced_and_persistent(sales_db):
    first = sales.assign_new(companies(), path=sales_db)
    assert len(first) == 10
    with sales.connect(sales_db) as conn:
        counts = dict(conn.execute("SELECT username,COUNT(*) FROM company_assignment GROUP BY username"))
        original = dict(conn.execute("SELECT inn,username FROM company_assignment"))
    assert abs(counts["user1"] - counts["user2"]) <= 1
    assert sales.assign_new(list(reversed(companies())), path=sales_db) == {}
    with sales.connect(sales_db) as conn:
        assert dict(conn.execute("SELECT inn,username FROM company_assignment")) == original


def test_only_new_companies_are_assigned(sales_db):
    sales.assign_new(companies(8), path=sales_db)
    before = {}
    with sales.connect(sales_db) as conn:
        before = dict(conn.execute("SELECT inn,username FROM company_assignment"))
    assigned = sales.assign_new(companies(11), path=sales_db)
    assert len(assigned) == 3
    with sales.connect(sales_db) as conn:
        after = dict(conn.execute("SELECT inn,username FROM company_assignment"))
        counts = dict(conn.execute("SELECT username,COUNT(*) FROM company_assignment GROUP BY username"))
    assert all(after[inn] == owner for inn, owner in before.items())
    assert abs(counts["user1"] - counts["user2"]) <= 1


def test_user_cannot_access_other_assignment(sales_db):
    sales.assign_new(companies(2), path=sales_db)
    with sales.connect(sales_db) as conn:
        assignments = dict(conn.execute("SELECT inn,username FROM company_assignment"))
        for inn, owner in assignments.items():
            assert sales.allowed(conn, user(sales_db, owner), inn)
            other = "user2" if owner == "user1" else "user1"
            assert not sales.allowed(conn, user(sales_db, other), inn)
            assert sales.allowed(conn, user(sales_db, "admin"), inn)


def test_comments_persist_and_cannot_be_edited_by_other_user(sales_db):
    sales.assign_new(companies(2), path=sales_db)
    with sales.connect(sales_db) as conn:
        inn, owner = conn.execute("SELECT inn,username FROM company_assignment LIMIT 1").fetchone()
    owner_user = user(sales_db, owner)
    sales.save_call(owner_user, inn, "interested", comment="Перезвонить после обеда", path=sales_db)
    with sales.connect(sales_db) as conn:
        comment = conn.execute("SELECT * FROM company_comment WHERE inn=?", (inn,)).fetchone()
        assert comment["body"] == "Перезвонить после обеда"
        assert conn.execute("SELECT call_result FROM company_state WHERE inn=?", (inn,)).fetchone()[0] == "interested"
    other = user(sales_db, "user2" if owner == "user1" else "user1")
    with pytest.raises(PermissionError):
        sales.edit_comment(other, comment["id"], "Чужая правка", path=sales_db)
    with pytest.raises(PermissionError):
        sales.edit_comment(user(sales_db, "admin"), comment["id"], "Правка администратора", path=sales_db)


def test_phone_and_multivalue_normalization():
    assert sales.normalize_phone("8 (999) 123-45-67") == "+79991234567"
    assert sales.normalize_phone("+7 999 123 45 67") == "+79991234567"
    assert sales.split_values("ABB | Wilo\nABB") == ["ABB", "Wilo"]


def test_source_database_is_not_modified(tmp_path):
    source = tmp_path / "centrifugal.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE company(inn TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO company VALUES('7700000000')")
    digest = source.read_bytes()
    sales_path = tmp_path / "sales.db"
    sales.init_schema(sales_path)
    assert source.read_bytes() == digest
