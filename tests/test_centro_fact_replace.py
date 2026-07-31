from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from app.tools.replace_centro_facts import process


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8-sig")
    return path


def _database(path: Path) -> Path:
    # sqlite3.Connection как context manager завершает транзакцию, но не
    # закрывает файловый дескриптор. closing обязателен для тестов на Windows,
    # иначе последующее атомарное переименование SQLite получает WinError 32.
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE company(
              inn TEXT PRIMARY KEY,
              predpriyatie TEXT,
              n_facts INTEGER NOT NULL DEFAULT 0,
              search_blob TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE contact(
              id INTEGER PRIMARY KEY,
              inn TEXT,
              value TEXT
            );
            CREATE TABLE fact(
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
            CREATE TABLE import_info(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO company(inn,predpriyatie) VALUES('1234567890','Тестовый завод');
            INSERT INTO contact(inn,value) VALUES('1234567890','+79990000000');
            INSERT INTO fact(inn,model,medium) VALUES('1234567890','СТАРЫЙ НАСОС','воздух');
            """
        )
        conn.commit()
    return path


def test_replaces_only_facts_and_preserves_conclusion(tmp_path):
    database = _database(tmp_path / "centrifugal.db")
    facts = _write(
        tmp_path / "SVOD-tri-sostoyaniya.csv",
        "inn;predpriyatie;sostoyanie;sreda;marki;srok_sluzhby;"
        "vyvod_ekspertizy;data;chto_za_data;chem_dokazano;tekst;ssylka;istochnik\n"
        "1234567890;Завод;эксплуатируется;воздух;К-250;20 лет;соответствует;"
        "31.07.2026;дата заключения экспертизы;заключение ЭПБ;компрессор К-250;"
        "https://example.test/air;ЭПБ\n"
        "1234567890;Завод;эксплуатируется;газ или иная среда;ГТК-10;;;"
        "30.07.2026;дата процедуры;тендер;газовый компрессор;"
        "https://example.test/gas;тендер\n"
        "1234567890;Завод;эксплуатируется;газ или иная среда;К-СМЕШ;;;"
        "29.07.2026;дата процедуры;паспорт;компрессор для газа и воздуха;"
        "https://example.test/mixed;паспорт\n"
        "1234567890;Завод;эксплуатируется;среда не названа;Н-370;;;"
        "28.07.2026;когда увидели;паспорт;центробежный насос;"
        "https://example.test/pump;паспорт\n"
        "1234567890;Завод;эксплуатируется;среда не названа;К-500;;;"
        "27.07.2026;когда увидели;паспорт;компрессор, среда не указана;"
        "https://example.test/unknown;паспорт\n"
        "1234567890;Завод;эксплуатируется;воздух;К-250;20 лет;соответствует;"
        "31.07.2026;дата заключения экспертизы;заключение ЭПБ;компрессор К-250;"
        "https://example.test/air;ЭПБ\n"
        "9999999999;Чужой завод;эксплуатируется;воздух;К-999;;;"
        "31.07.2026;дата;паспорт;компрессор;https://example.test/other;паспорт\n",
    )

    preview = process(database, facts, dry_run=True)
    assert preview["kept"] == 3
    assert preview["excluded_non_air"] == 1
    assert preview["excluded_pump"] == 1
    assert preview["duplicates"] == 1
    assert preview["outside_company_list"] == 1

    result = process(database, facts)
    assert result["kept"] == 3

    with closing(sqlite3.connect(database)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM fact ORDER BY model").fetchall()
        assert {row["model"] for row in rows} == {"К-250", "К-500", "К-СМЕШ"}
        air = next(row for row in rows if row["model"] == "К-250")
        assert air["commission_conclusion"] == "соответствует"
        assert "Заключение комиссии: соответствует" in air["evidence"]
        assert air["service_life"] == "20 лет"
        assert air["date_kind"] == "дата заключения экспертизы"
        assert conn.execute("SELECT n_facts FROM company").fetchone()[0] == 3
        assert "к-250" in conn.execute("SELECT search_blob FROM company").fetchone()[0]
        assert conn.execute(
            "SELECT value FROM import_info WHERE key='facts_source_kind'"
        ).fetchone()[0] == "SVOD-tri-sostoyaniya-filtered"
