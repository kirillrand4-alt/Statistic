"""Тесты базы обзвона: парсинг выгрузок, импорт с дедупом, очередь, удаление."""
from __future__ import annotations

import io

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app.services import callbase


def _tsv(rows: list[list[str]]) -> bytes:
    return ("\n".join("\t".join(r) for r in rows)).encode("utf-8")


HEAD = ["ИНН", "Краткое", "Полное", "Статус", "Адрес", "Телефоны", "Emails",
        "Сайты", "Выручка"]
ROW_A = ["3827013097", 'АО "ИМК"', "АО ИРКУТСКАЯ МК", "Действующая компания",
         "664043, Иркутская область, г. Иркутск", "+7 395 298-61-70 | +7 395 243-79-97",
         "imk38@mail.ru", "https://imk-38.ru | https://yastatic.net/ | https://mc.yandex.ru",
         "295,2 млн руб."]
ROW_B = ["6657004027", 'ООО "СТУМЗ"', "ООО СТАРОУТКИНСКИЙ МЗ", "Действующая компания",
         "623036, Свердловская область", "+7 343 585-51-79", "zavod@stumz.ru",
         "https://stumz.ru", "130,5 млн руб."]
ROW_NOPHONE = ["7705488260", 'ООО "ТЕХЭКСПОРТ"', "ООО ТЕХЭКСПОРТ", "Действующая компания",
               "117545, г. Москва", "", "", "", "2,2 млрд руб."]


def test_parse_money():
    assert callbase.parse_money("55,3 млрд руб.") == pytest.approx(55.3e9)
    assert callbase.parse_money("295,2 млн руб.") == pytest.approx(295.2e6)
    assert callbase.parse_money("424 тыс. руб.") == pytest.approx(424e3)
    assert callbase.parse_money("0 руб.") == 0
    assert callbase.parse_money("") is None
    assert callbase.parse_money(None) is None
    assert callbase.parse_money(12345) == 12345


def test_clean_sites_drops_junk():
    cleaned = callbase.clean_sites(
        "https://checko.ru/company/x | https://yastatic.net/ | https://imk-38.ru"
        " | https://mc.yandex.ru | https://an.yandex.ru/ | http://nitrosplav.ru")
    assert cleaned == "https://imk-38.ru | http://nitrosplav.ru"


def test_parse_tsv_and_import_dedup(db):
    rows = callbase.parse_upload("base.tsv", _tsv([HEAD, ROW_A, ROW_B]))
    assert len(rows) == 2
    assert rows[0]["inn"] == "3827013097"
    assert rows[0]["revenue_num"] == pytest.approx(295.2e6)
    assert "yastatic" not in rows[0]["sites"]

    added, skipped = callbase.import_rows(db, "kc", rows)
    assert (added, skipped) == (2, 0)
    # повторная загрузка того же файла — все дубли по ИНН
    added2, skipped2 = callbase.import_rows(db, "kc", rows)
    assert (added2, skipped2) == (0, 2)
    # в другую базу импортируется независимо
    added3, _ = callbase.import_rows(db, "meyer", rows[:1])
    assert added3 == 1
    assert callbase.count(db, "kc") == 2
    assert callbase.count(db, "meyer") == 1


def test_parse_xlsx_finds_company_sheet_and_priorities(db):
    wb = openpyxl.Workbook()
    ws0 = wb.active  # первый лист без компаний (как матрица ОКВЭД)
    ws0.title = "ОКВЭД и оборудование"
    ws0.append(["Матрица", ""])
    ws = wb.create_sheet("Лист1")
    ws.append(HEAD + ["Итоговый балл приоритета", "Оборудование по основному ОКВЭД",
                      "Выручка, руб. (расчет)", "Приоритет × выручка / 10000"])
    ws.append(ROW_A + ["50", "Промышленные компрессоры от 200 000 ₽", "295200000", "1476000"])
    ws.append(ROW_B + ["28", "Промышленные компрессоры от 200 000 ₽", "130500000", "365400"])
    buf = io.BytesIO()
    wb.save(buf)

    rows = callbase.parse_upload("prio.xlsx", buf.getvalue())
    assert len(rows) == 2
    assert rows[0]["priority"] == 50
    assert rows[0]["rank_metric"] == pytest.approx(1476000)
    callbase.import_rows(db, "kc", rows)

    # очередь: rank_metric по убыванию
    first, total = callbase.pick(db, "kc")
    assert total == 2
    assert first.inn == "3827013097"
    nxt, _ = callbase.pick(db, "kc", skip=1)
    assert nxt.inn == "6657004027"


def test_queue_filters_and_revenue_order_without_priorities(db):
    rows = callbase.parse_upload("b.tsv", _tsv([HEAD, ROW_A, ROW_B, ROW_NOPHONE]))
    callbase.import_rows(db, "kc", rows)
    # без колонок приоритета очередь = по выручке; only_phone режет безтелефонных
    first, total = callbase.pick(db, "kc", only_phone=True)
    assert total == 2 and first.inn == "3827013097"  # 295 млн > 130 млн
    _, total_all = callbase.pick(db, "kc", only_phone=False)
    assert total_all == 3  # ТЕХЭКСПОРТ (2,2 млрд) без телефона
    first_all, _ = callbase.pick(db, "kc", only_phone=False)
    assert first_all.inn == "7705488260"
    # фильтр по региону и мин. выручке
    _, n_irk = callbase.pick(db, "kc", region="Иркутск")
    assert n_irk == 1
    _, n_rich = callbase.pick(db, "kc", only_phone=False, min_revenue_mln=1000)
    assert n_rich == 1


def test_delete_logs_and_removes(db, tmp_path, monkeypatch):
    monkeypatch.setattr(callbase, "DATA_DIR", str(tmp_path))
    rows = callbase.parse_upload("b.tsv", _tsv([HEAD, ROW_A, ROW_B]))
    callbase.import_rows(db, "kc", rows)
    first, total = callbase.pick(db, "kc")
    assert total == 2
    assert callbase.delete_company(db, "kc", first.id)
    nxt, total2 = callbase.pick(db, "kc")
    assert total2 == 1 and nxt.inn != first.inn
    log = tmp_path / "deleted_kc.tsv"
    assert log.exists()
    body = log.read_text(encoding="utf-8")
    assert "3827013097" in body and body.startswith("inn\t")
    # повторное удаление того же id — False, второй раз в лог не пишется
    assert not callbase.delete_company(db, "kc", first.id)


def test_calls_page_endpoints(db, tmp_path, monkeypatch):
    monkeypatch.setattr(callbase, "DATA_DIR", str(tmp_path))
    from app.main import app
    client = TestClient(app)

    r = client.get("/calls/kc")
    assert r.status_code == 200
    assert "База пуста" in r.text
    assert client.get("/calls/nope").status_code == 404

    # загрузка tsv через форму
    r = client.post("/ui/calls/kc/upload",
                    files={"file": ("base.tsv", _tsv([HEAD, ROW_A, ROW_B]), "text/tab-separated-values")},
                    follow_redirects=True)
    assert r.status_code == 200
    assert "Импортировано 2" in r.text
    assert "ИМК" in r.text and "tel:+73952986170" in r.text

    # удалить текущую -> следующая
    company, _ = callbase.pick(db, "kc")
    r = client.post("/ui/calls/kc/delete", data={"company_id": company.id, "only_phone": 1,
                                                 "active_only": 1},
                    follow_redirects=True)
    assert r.status_code == 200
    assert "СТУМЗ" in r.text and "ИМК" not in r.text

    # очистить базу
    r = client.post("/ui/calls/kc/clear", follow_redirects=True)
    assert "База очищена (удалено 1)" in r.text
