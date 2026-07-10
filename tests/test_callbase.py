"""Тесты базы обзвона: парсинг выгрузок, импорт с дедупом, очередь, удаление."""
from __future__ import annotations

import io

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app.services import callbase


@pytest.fixture(autouse=True)
def _fresh_queue_cache():
    callbase._bump_version()  # кэш очереди живёт на процесс, БД пересоздаётся на тест
    yield


def _tsv(rows: list[list[str]]) -> bytes:
    return ("\n".join("\t".join(r) for r in rows)).encode("utf-8")


HEAD = ["ИНН", "Краткое", "Полное", "Статус", "Адрес", "Телефоны", "Emails",
        "Сайты", "Выручка"]
ROW_A = ["3827013097", 'АО "ИМК"', "АО ИРКУТСКАЯ МК", "Действующая компания",
         "664043, Иркутская область, г. Иркутск", "+7 395 298-61-70 | +7 395 243-79-97",
         "imk38@mail.ru", "https://imk-38.ru | https://yastatic.net/ | https://mc.yandex.ru",
         "295,2 млн руб."]
ROW_B = ["6657004027", 'ООО "СТУМЗ"', "ООО СТАРОУТКИНСКИЙ МЗ", "Действующая компания",
         "623036, Свердловская область", "+7 343 585-51-79 | +7 908 907-92-00", "zavod@stumz.ru",
         "https://stumz.ru", "130,5 млн руб."]
ROW_NOPHONE = ["7705488260", 'ООО "ТЕХЭКСПОРТ"', "ООО ТЕХЭКСПОРТ", "Действующая компания",
               "117545, г. Москва", "", "", "", "2,2 млрд руб."]


def test_region_and_mobile_helpers():
    assert callbase.region_from_address("664043, Иркутская область, г. Иркутск, ул. Ракитная") == "Иркутская область"
    assert callbase.region_from_address("117545, г. Москва, вн. тер. г. муниципальный округ") == "Москва"
    assert callbase.region_from_address("423603, Республика Татарстан, м. р-н Елабужский") == "Республика Татарстан"
    assert callbase.region_from_address("") == ""
    assert callbase.has_mobile("+7 908 907-92-00")
    assert callbase.has_mobile("+7 343 585-51-79 | 89631112233")
    assert not callbase.has_mobile("+7 343 585-51-79")
    assert not callbase.has_mobile("")


def test_extract_site_contacts_unique():
    ph, em = callbase.extract_site_contacts(
        "+7 (495) 785-94-60, 8 800 511-41-35 | info@Zavod.ru; INFO@zavod.ru, +7 495 785-94-60")
    assert ph == ["+74957859460", "+78005114135"]   # канон, дубль схлопнут
    assert em == ["info@zavod.ru"]                    # нижний регистр, дубль убран
    assert callbase.extract_site_contacts("") == ([], [])
    assert callbase.extract_site_contacts(None) == ([], [])
    # цифры внутри email не считаем телефоном
    assert callbase.extract_site_contacts("a1234567890@x.ru")[0] == []


def test_parse_site_contacts_column(db):
    head = ["ИНН", "Краткое", "Полное", "Статус", "Адрес", "Телефоны", "Emails", "Сайты",
            "Выручка", "Уникальные контакты с сайта компании"]
    row = ["7705488260", 'ООО "Т"', "ООО ТЕХ", "Действующая компания", "117545, г. Москва",
           "+7 495 111-22-33", "reg@t.ru", "https://t.ru", "2,2 млрд руб.",
           "+7 (499) 700-10-20 site@t.ru | 8-916-000-11-22"]
    rows = callbase.parse_upload("b.tsv", _tsv([head, row]))
    r = rows[0]
    assert r["site_phones"] == "+74997001020 | +79160001122"
    assert r["site_emails"] == "site@t.ru"
    # реестровые «Телефоны/Emails/Сайты» не затронуты, URL-колонку не приняли за контакты
    assert r["phones"] == "+7 495 111-22-33" and r["emails"] == "reg@t.ru"
    assert r["sites"] == "https://t.ru"
    callbase.import_rows(db, "kc", rows)
    c, _ = callbase.pick(db, "kc")
    assert c.site_phones == "+74997001020 | +79160001122"


def test_parse_site_contacts_separate_columns(db):
    head = ["ИНН", "Краткое", "Статус", "Адрес", "Телефоны", "Выручка",
            "Телефоны с сайта", "E-mail с сайта"]
    row = ["1", "X", "Действующая компания", "117545, г. Москва", "+7 495 111-22-33",
           "1 млн руб.", "8 916 000 11 22", "hello@x.ru | dup@x.ru | HELLO@x.ru"]
    rows = callbase.parse_upload("b.tsv", _tsv([head, row]))
    assert rows[0]["site_phones"] == "+79160001122"
    assert rows[0]["site_emails"] == "hello@x.ru | dup@x.ru"


def test_ensure_schema_adds_site_contact_columns(db):
    from sqlalchemy import inspect
    callbase.ensure_schema(db)  # на актуальной схеме — no-op, но идемпотентно
    cols = {c["name"] for c in inspect(db.get_bind()).get_columns("call_company")}
    assert {"site_phones", "site_emails", "region", "max_hit"} <= cols


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
                      "Выручка, руб. (расчет)", "Приоритет × выручка / 10000",
                      "Макс. балл по одной связке"])
    ws.append(ROW_A + ["50", "Промышленные компрессоры от 200 000 ₽", "295200000", "1476000", "5"])
    ws.append(ROW_B + ["28", "Промышленные компрессоры от 200 000 ₽", "130500000", "365400", "3"])
    buf = io.BytesIO()
    wb.save(buf)

    rows = callbase.parse_upload("prio.xlsx", buf.getvalue())
    assert len(rows) == 2
    assert rows[0]["priority"] == 50 and rows[0]["max_hit"] == 5
    assert rows[0]["rank_metric"] == pytest.approx(1476000)
    callbase.import_rows(db, "kc", rows)

    # очередь: rank_metric по убыванию
    first, total = callbase.pick(db, "kc")
    assert total == 2
    assert first.inn == "3827013097"
    nxt, _ = callbase.pick(db, "kc", skip=1)
    assert nxt.inn == "6657004027"
    # балл связки «встретился хотя бы раз»: от/до
    _, n5 = callbase.pick(db, "kc", hit_from=5)
    assert n5 == 1
    _, n35 = callbase.pick(db, "kc", hit_from=3, hit_to=4)
    assert n35 == 1  # только СТУМЗ (3)
    # общий приоритет от/до
    _, nr = callbase.pick(db, "kc", rank_from=1000000)
    assert nr == 1
    _, nr2 = callbase.pick(db, "kc", rank_from=100000, rank_to=500000)
    assert nr2 == 1
    # категории оборудования для выпадающего списка
    eqs = callbase.equipments(db, "kc")
    assert eqs and eqs[0][0].startswith("Промышленные компрессоры") and eqs[0][1] == 2


def test_queue_filters_and_revenue_order_without_priorities(db):
    rows = callbase.parse_upload("b.tsv", _tsv([HEAD, ROW_A, ROW_B, ROW_NOPHONE]))
    assert rows[0]["region"] == "Иркутская область"  # регион распарсен при импорте
    callbase.import_rows(db, "kc", rows)
    # без колонок приоритета очередь = по выручке; only_phone режет безтелефонных
    first, total = callbase.pick(db, "kc", only_phone=True)
    assert total == 2 and first.inn == "3827013097"  # 295 млн > 130 млн
    _, total_all = callbase.pick(db, "kc", only_phone=False)
    assert total_all == 3  # ТЕХЭКСПОРТ (2,2 млрд) без телефона
    first_all, _ = callbase.pick(db, "kc", only_phone=False)
    assert first_all.inn == "7705488260"
    # выпадающие списки значений
    assert ("Иркутская область", 1) in callbase.regions(db, "kc")
    assert any("24.10" in o for o, _n in callbase.okveds(db, "kc")) or callbase.okveds(db, "kc") == []
    # фильтры: регион (точное значение из списка), выручка от/до, сотовые
    _, n_irk = callbase.pick(db, "kc", region="Иркутская область")
    assert n_irk == 1
    _, n_rich = callbase.pick(db, "kc", only_phone=False, rev_from=1000)
    assert n_rich == 1
    _, n_mid = callbase.pick(db, "kc", only_phone=False, rev_from=100, rev_to=500)
    assert n_mid == 2  # 295,2 и 130,5 млн
    mob, n_mob = callbase.pick(db, "kc", mobile_only=True)
    assert n_mob == 1 and mob.inn == "6657004027"  # +7 908 … — сотовый


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


def test_ensure_schema_backfills_region(db):
    from app.db.models import CallCompany
    db.add(CallCompany(base="kc", inn="1", name_short="X",
                       address="664043, Иркутская область, г. Иркутск", region=None))
    db.commit()
    callbase.ensure_schema(db)  # на живой схеме — no-op по колонкам + backfill региона
    row = db.query(CallCompany).filter_by(inn="1").one()
    assert row.region == "Иркутская область"


def test_obzvon_endpoints(db, tmp_path, monkeypatch):
    monkeypatch.setattr(callbase, "DATA_DIR", str(tmp_path))
    from app.obzvon import app as obz_app
    auth = ("test", "test")
    client = TestClient(obz_app)

    # отдельные пароли: без них 401, с неверными 401, с верными 200
    assert client.get("/obzvon/kc").status_code == 401
    assert client.get("/obzvon/kc", auth=("test", "wrong")).status_code == 401
    r = client.get("/obzvon/kc", auth=auth)
    assert r.status_code == 200
    # каркас мгновенный: карточка не в нём, а во фрагменте /card
    assert 'id="card"' in r.text and "Загружаю карточку" in r.text
    assert client.get("/obzvon/nope", auth=auth).status_code == 404
    r = client.get("/obzvon/kc/card", auth=auth)
    assert r.status_code == 200 and "База пуста" in r.text
    # корень ведёт на первую базу
    r = client.get("/obzvon/", auth=auth, follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/obzvon/kc"
    # роутов основного сервиса в этом приложении нет
    assert client.get("/", auth=auth, follow_redirects=True).status_code == 200  # редирект в обзвон
    assert client.get("/keywords", auth=auth).status_code == 404

    # загрузка tsv через форму
    r = client.post("/obzvon/kc/upload", auth=auth,
                    files={"file": ("base.tsv", _tsv([HEAD, ROW_A, ROW_B]), "text/tab-separated-values")},
                    follow_redirects=True)
    assert r.status_code == 200
    assert "Импортировано 2" in r.text
    card = client.get("/obzvon/kc/card", auth=auth).text
    assert "ИМК" in card and "tel:+73952986170" in card
    # выпадающие списки приходят JSON'ом внутри фрагмента
    assert 'id="filters-data"' in card and "Иркутская область" in card
    # ссылок/навигации основного сервиса нет ни в каркасе, ни во фрагменте
    assert "Дашборд" not in r.text and "SEO Статистика" not in r.text
    assert "Дашборд" not in card

    # продажник: карточку видит, загрузку/очистку — нет (ни кнопок, ни эндпоинтов)
    seller = ("seller", "sell")
    card = client.get("/obzvon/kc/card", auth=seller).text
    assert "ИМК" in card
    assert "Импортировать" not in card and "Очистить базу" not in card
    assert client.post("/obzvon/kc/upload", auth=seller,
                       files={"file": ("b.tsv", _tsv([HEAD, ROW_A]), "text/plain")}).status_code == 403
    assert client.post("/obzvon/kc/clear", auth=seller).status_code == 403
    # админ кнопки загрузки видит
    assert "Импортировать" in client.get("/obzvon/kc/card", auth=auth).text

    # удалить текущую («обзвонили») может и продажник -> во фрагменте следующая
    company, _ = callbase.pick(db, "kc")
    r = client.post("/obzvon/kc/delete", auth=seller,
                    data={"company_id": company.id, "only_phone": 1, "active_only": 1},
                    follow_redirects=True)
    assert r.status_code == 200
    card = client.get("/obzvon/kc/card", auth=seller).text
    assert "СТУМЗ" in card and "ИМК" not in card

    # пропуск дальше конца очереди -> фрагмент начинает сначала (с пометкой)
    card = client.get("/obzvon/kc/card?skip=99", auth=auth).text
    assert "показываю сначала" in card and "СТУМЗ" in card

    # очистить базу (админ)
    r = client.post("/obzvon/kc/clear", auth=auth, follow_redirects=True)
    assert "База очищена (удалено 1)" in r.text


def test_obzvon_parse_users():
    from app.obzvon import parse_users
    assert parse_users("vasya:p1, petya:p2;dima:p:3") == {
        "vasya": "p1", "petya": "p2", "dima": "p:3"}
    assert parse_users("") == {}
    assert parse_users("bad") == {}


def test_page_endpoint_tolerates_empty_number_filters(db):
    # регресс бага 422: авто-сабмит формы шлёт пустые числовые поля как "" —
    # эндпоинт-каркас должен отдавать 200, а не падать на float("").
    from app.obzvon import app as obz_app
    client = TestClient(obz_app)
    auth = ("test", "test")
    empty = {"f": 1, "hit_from": "", "hit_to": "", "rank_from": "", "rank_to": "",
             "rev_from": "", "rev_to": "", "only_phone": 1, "active_only": 1}
    assert client.get("/obzvon/kc", params=empty, auth=auth).status_code == 200
    # дробное значение тоже принимается
    assert client.get("/obzvon/kc", params={**empty, "rev_from": "2.5"},
                      auth=auth).status_code == 200
    # /card тоже терпит пустые числовые/булевы параметры (паритет, не 422)
    for p in ("hit_from", "rank_from", "rev_to", "only_phone", "active_only", "mobile_only"):
        assert client.get("/obzvon/kc/card", params={p: ""}, auth=auth).status_code == 200, p


def test_shell_card_url_not_html_escaped(db, tmp_path, monkeypatch):
    # регресс: card_qs в JS-строке не должен экранироваться (&amp;), иначе браузер
    # шлёт "amp;skip" и пропуск/фильтры (кроме первого) не работают
    import re
    monkeypatch.setattr(callbase, "DATA_DIR", str(tmp_path))
    from app.obzvon import app as obz_app
    client = TestClient(obz_app)
    auth = ("test", "test")
    client.post("/obzvon/kc/upload", auth=auth,
                files={"file": ("b.tsv", _tsv([HEAD, ROW_A, ROW_B]), "text/plain")})
    shell = client.get("/obzvon/kc", params={"skip": 1}, auth=auth).text
    url = re.search(r'var url = "([^"]*)"', shell).group(1)
    assert "&amp;" not in url and "amp;skip" not in url
    assert "&skip=1" in url
    # и функционально: этот url отдаёт ВТОРУЮ компанию (пропуск сработал)
    card = client.get(url, auth=auth).text
    assert "СТУМЗ" in card and "ИМК" not in card


def test_basic_auth_non_ascii_password():
    # регресс бага 500: кириллический пароль не должен ронять compare_digest
    import base64
    from app.obzvon import BasicAuthASGI

    m = BasicAuthASGI(None, {"vasya": "секрет"})

    def hdr(u, p):
        return b"Basic " + base64.b64encode(f"{u}:{p}".encode("utf-8"))

    assert m._ok(hdr("vasya", "секрет")) is True     # верный кирилл-пароль пускает
    assert m._ok(hdr("vasya", "wrong")) is False
    assert m._ok(hdr("ghost", "секрет")) is False    # неизвестный логин — без краша


def test_delete_confirm_js_escaped(db, tmp_path, monkeypatch):
    # регресс бага 5: апостроф в названии не должен ломать confirm() (JS-escape)
    monkeypatch.setattr(callbase, "DATA_DIR", str(tmp_path))
    from app.obzvon import app as obz_app
    client = TestClient(obz_app)
    auth = ("test", "test")
    head = ["ИНН", "Краткое", "Статус", "Адрес", "Телефоны", "Выручка"]
    row = ["1", "О'КЕЙ", "Действующая компания", "117545, г. Москва", "+7 495 111-22-33", "1 млн руб."]
    client.post("/obzvon/kc/upload", auth=auth,
                files={"file": ("b.tsv", _tsv([head, row]), "text/plain")})
    card = client.get("/obzvon/kc/card", auth=auth).text
    assert 'confirm("Удалить «"' in card                 # новый безопасный формат
    assert "confirm('Удалить «О'КЕЙ" not in card         # старый ломающийся — нет


def test_csrf_guard_on_destructive_posts(db, tmp_path, monkeypatch):
    # регресс бага 6: cross-site POST на разрушающие роуты отклоняется
    monkeypatch.setattr(callbase, "DATA_DIR", str(tmp_path))
    from app.obzvon import app as obz_app
    client = TestClient(obz_app)
    auth = ("test", "test")
    assert client.post("/obzvon/kc/clear", auth=auth,
                       headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert client.post("/obzvon/kc/clear", auth=auth,
                       headers={"origin": "http://evil.example"}).status_code == 403
    # свой origin — проходит (303 редирект)
    r = client.post("/obzvon/kc/clear", auth=auth,
                    headers={"sec-fetch-site": "same-origin"}, follow_redirects=False)
    assert r.status_code == 303


def test_delete_backup_has_region_and_maxhit(db, tmp_path, monkeypatch):
    # регресс бага 8: бэкап удалённой строки не должен терять region/max_hit
    monkeypatch.setattr(callbase, "DATA_DIR", str(tmp_path))
    from app.db.models import CallCompany
    c = CallCompany(base="kc", inn="1", name_short="X", region="Москва",
                    max_hit=5, address="117545, г. Москва")
    db.add(c)
    db.commit()
    assert callbase.delete_company(db, "kc", c.id)
    header = (tmp_path / "deleted_kc.tsv").read_text(encoding="utf-8").splitlines()[0]
    assert "region" in header and "max_hit" in header


def test_aggregation_cache_invalidates_on_import(db):
    # регресс: списки регионов/оборудования кешируются, но обновляются при импорте
    head = ["ИНН", "Краткое", "Статус", "Адрес", "Выручка"]
    callbase.import_rows(db, "kc", callbase.parse_upload(
        "a.tsv", _tsv([head, ["1", "A", "Действующая компания", "117545, г. Москва", "1 млн руб."]])))
    assert [r for r, _ in callbase.regions(db, "kc")] == ["Москва"]
    callbase.import_rows(db, "kc", callbase.parse_upload(
        "b.tsv", _tsv([head, ["2", "B", "Действующая компания", "664043, Иркутская область, г. Иркутск", "1 млн руб."]])))
    regs = {r for r, _ in callbase.regions(db, "kc")}
    assert regs == {"Москва", "Иркутская область"}  # кеш сбросился, новый регион виден
