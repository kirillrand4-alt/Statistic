"""Цена товара на нашей карточке — только из блока цены, не из «похожих товаров».

Разметка prokompressor: у товара с ценой стоит
    <div class="price font-bold font_mxs" data-currency="RUB" data-value="261824">
    <span class="price_value">261 824 руб.</span>
у товара без цены — тот же блок, но с текстом «Цена по запросу».
Соседние товары лежат в `dm-seo-similar-card__price`, и именно их числа сбивали
и main_price (взяла 10 334 289 у Enger LUF90W — цену дожимного поршневого из подборки),
и мой первый проход по «любому числу рядом с ₽».
"""
import re

_PRICE_BLOCK = re.compile(r'class="price[^"]*"[^>]*data-value="(\d+)"')
_PRICE_VALUE = re.compile(r'class="price_value"[^>]*>\s*([\d\s\xa0]+)')
_ON_REQUEST = re.compile(r'class="price[^"]*"[^>]*>\s*Цена по запросу', re.I)


def our_price(page: str):
    """(цена|None, причина). None + «по запросу» — цены на сайте нет."""
    m = _PRICE_BLOCK.search(page)
    if m:
        return int(m.group(1)), "цена в блоке товара"
    m = _PRICE_VALUE.search(page)
    if m:
        return int(re.sub(r"\D", "", m.group(1))), "цена в блоке товара"
    if _ON_REQUEST.search(page):
        return None, "цена по запросу"
    if re.search(r"<h1[^>]*>\s*404", page):
        return None, "404"
    return None, "блока цены нет"
