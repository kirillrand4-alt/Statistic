from __future__ import annotations

from pathlib import Path

from app.services import centro_catalog
from app.services import centro_medium
from app.tools.build_centro_db import build


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8-sig")
    return path


def test_medium_filter_is_conservative():
    assert not centro_medium.is_definitely_non_air_medium("")
    assert not centro_medium.is_definitely_non_air_medium("неизвестно")
    assert not centro_medium.is_definitely_non_air_medium("сжатый воздух")
    assert not centro_medium.is_definitely_non_air_medium("газ и воздух")
    assert not centro_medium.is_definitely_non_air_medium("газовоздушная смесь")
    assert centro_medium.is_definitely_non_air_medium("природный газ")
    assert centro_medium.is_definitely_non_air_medium("азот")
    assert centro_medium.is_pump_fact(equipment_type="центробежный насос")
    assert not centro_medium.is_pump_fact(equipment_type="центробежный компрессор")


def test_builds_detailed_centro_snapshot(tmp_path, monkeypatch):
    summary = _write(
        tmp_path / "SVOD.csv",
        "inn;predpriyatie;tipy_mashin;sreda;marki;sostoyaniya_po_faktam;"
        "vyvod_ekspertizy;data_zakluchenia;status_egrul;novost;novost_ssylka;"
        "telefony_iz_bazy;telefony_predpriyatiya;nomera_bez_vladelca;pochta;"
        "istochnik_dopolneniya;proverok_nadzora;ssylki_na_istochniki\n"
        "1234567890;Тестовый завод;центробежная;воздух;К-250;есть;"
        "соответствует;31.07.2026;ACTIVE;Модернизация;https://news.example/item;"
        "+7 999 000-00-01;;;;office@example.ru;директор/ЕГРЮЛ;2;"
        "https://monitor-pb.example/check\n",
    )
    details = _write(
        tmp_path / "POLNYY.csv",
        "razdel;inn;predpriyatie;chto;kto_ili_marka;dolzhnost_ili_tip;rol;"
        "nomer_10cifr;vid_nomera;pochta;data;chem_dokazano;citata;ssylka;"
        "istochnik;tehLPR\n"
        "ФАКТ;1234567890;Тестовый завод;есть;К-250;центробежный компрессор;"
        "воздух;;;;2025-01-01;паспорт оборудования;модель К-250;"
        "https://source.example/fact;паспорт;\n"
        "ФАКТ;1234567890;Тестовый завод;есть;К-ГАЗ;центробежный компрессор;"
        "природный газ;;;;2025-01-02;паспорт;газовый компрессор;"
        "https://source.example/gas;паспорт;\n"
        "ФАКТ;1234567890;Тестовый завод;есть;К-СМЕШ;центробежный компрессор;"
        "газ и воздух;;;;2025-01-03;паспорт;две среды;"
        "https://source.example/mixed;паспорт;\n"
        "ФАКТ;1234567890;Тестовый завод;есть;К-НЕИЗВ;центробежный компрессор;"
        "неизвестно;;;;2025-01-04;паспорт;среда не указана;"
        "https://source.example/unknown;паспорт;\n"
        "ФАКТ;1234567890;Тестовый завод;есть;Н-1;центробежный насос;"
        "воздух;;;;2025-01-05;паспорт;насос;"
        "https://source.example/pump;паспорт;\n"
        "НОВОСТЬ;1234567890;Тестовый завод;модернизация;Новый цех;;;;;;"
        "2025-02-01;;запущен новый цех;https://source.example/news;СМИ;\n"
        "ЧЕЛОВЕК;1234567890;Тестовый завод;;Иванов Иван;главный энергетик;"
        "технический ЛПР;+7 999 000-00-02;мобильный;ivanov@example.ru;;;;"
        "https://source.example/person;сайт;да\n"
        "НОМЕР;1234567890;Тестовый завод;;;;;+7 999 000-00-03;общий;;;;;"
        "https://source.example/phone;справочник;\n",
    )
    contacts = _write(
        tmp_path / "CONTACTS.csv",
        "inn;predpriyatie;fio;dolzhnost;rol_kanon;tehLPR;mobilnyy_10cifr;"
        "vse_nomera;vid_nomera;pochta;istochnik;ssylka_na_istochnik\n"
        "1234567890;Тестовый завод;Петров Петр;начальник снабжения;закупки;"
        "нет;+7 999 000-00-04;;мобильный;petrov@example.ru;сайт;"
        "https://source.example/buyer\n",
    )
    output = tmp_path / "centrifugal.db"

    build(summary, details, contacts, output)
    monkeypatch.setenv("CENTRIFUGAL_DB", str(output))

    companies = centro_catalog.list_companies()
    assert len(companies) == 1
    assert companies[0]["inn"] == "1234567890"
    assert "1234567890" in companies[0]["search_blob"]
    assert companies[0]["n_facts"] == 3
    assert companies[0]["n_signals"] == 1
    assert companies[0]["vyvod_ekspertizy"] == "соответствует"
    assert companies[0]["data_zakluchenia"] == "31.07.2026"

    contact_rows = centro_catalog.contacts("1234567890")
    assert any(row["is_purchaser"] for row in contact_rows)
    assert any(row["has_role"] for row in contact_rows)
    assert any(not row["has_role"] for row in contact_rows)
    assert all(row["source_url"].startswith("https://") for row in contact_rows if row["source_url"])
    assert centro_catalog.role_phone_inns() == {"1234567890"}

    facts = centro_catalog.facts("1234567890")
    assert {row["model"] for row in facts} == {"К-250", "К-СМЕШ", "К-НЕИЗВ"}
    assert all(row["model"] not in {"К-ГАЗ", "Н-1"} for row in facts)
    assert next(row for row in facts if row["model"] == "К-250")["source_url"] == "https://source.example/fact"

    news = centro_catalog.signals("1234567890")
    assert news[0]["title"] == "Новый цех"
    assert news[0]["source_url"] == "https://source.example/news"

    people = centro_catalog.persons("1234567890")
    assert people[0]["is_tech"] == 1
    assert people[0]["phone"] == "+79990000002"

    sources = centro_catalog.company_sources("1234567890")
    assert any("директор" in row["field_name"] for row in sources)
    assert any("Проверки" in row["field_name"] for row in sources)

    info = centro_catalog.database_info()
    assert info["source_kind"] == "csv-import-v3-air-compressors"
    assert info["company_count"] == "1"
    assert info["fact_count"] == "3"
    assert info["excluded_fact_count"] == "2"
