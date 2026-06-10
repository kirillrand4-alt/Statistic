"""Единый список прогонов парсера (прайс+specs). Подключается из build_review и
спек-скриптов, чтобы новые выгрузки «закидывались» в одном месте.
Порядок ХРОНОЛОГИЧЕСКИЙ: при объединении specs по URL поздний прогон переписывает ключи."""

U = "/root/.claude/uploads/62a19005-a7bf-569b-926e-b59b4a62600d/"

# все выгрузки парсера (prices_checked = ручные прогоны пользователя; all_prices = ночные/общие)
SCRAPE_FILES = [
    U + "c7c60579-prices_checked_20260608.csv",
    U + "7fee0300-all_prices_20260609_102140.csv",
    U + "4b460a4b-prices_checked_20260609.csv",
    U + "f85cae40-all_prices_20260609_143339.csv",
    U + "e0e2167c-prices_checked_20260609_1.csv",
    U + "night_run/all_prices_20260610_014032.csv",
    U + "88a8f690-all_prices_20260610_035123.csv",
    U + "8c41d213-all_prices_20260610_054517.csv",
    U + "2ad0f5e3-all_prices_20260610_060124.csv",
    U + "9c5d90fd-all_prices_20260610_061533.csv",
    U + "c6dd7a65-all_prices_20260610_063956.csv",
    U + "5beded8c-all_prices_20260610_074407.csv",   # дочистка Atlas: прогон по atlas_competitors_need_specs
    U + "e6ea00d5-all_prices_20260610_173444.csv",   # дочистка ВСЕ бренды (из rar; 20k строк, specs 99%)
]

# прогоны ОБНОВЛЁННОГО парсера (фиксы проверены с 06:01 10.06, см. PARSER_NOTES).
# Если URL сканирован новым парсером и поле всё равно пустое — на странице данных нет,
# повторный прогон не поможет.
NEW_PARSER_FILES = [f for f in SCRAPE_FILES if f.split("-all_prices_")[-1] >= "20260610_060124"]
