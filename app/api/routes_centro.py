"""Страницы «Центробежные 1 / 2» — две НОВЫЕ базы обзвона поверх автономной БД.

Сервисный слой + маршруты в одном модуле: у обычных баз обзвона данные лежат в
общей таблице ``call_company`` (SQLAlchemy, app/services/callbase.py), а эти две
базы живут в ОТДЕЛЬНОМ файле ``centrifugal.db``, который собирается офлайн-скриптом
и панелью только читается. Городить ради read-only файла ORM-модель, вторую
SessionLocal и миграции незачем — весь доступ здесь укладывается в десяток
sqlite3-запросов, поэтому сервисный слой держим рядом с маршрутами.

Почему отдельный файл БД (требование владельца):
  * seo.db  — 11 ГБ, общая с SEO-аналитикой; класть туда базу продажников нельзя;
  * enrich.db — её ПИШЕТ боевое обогащение, конкурировать за блокировки нельзя.
Поэтому centrifugal.db открывается ТОЛЬКО на чтение (``mode=ro``), соединение
создаётся на запрос и закрывается в ``finally``: даже при пересборке файла
скриптом панель ничего не блокирует и не ломает.

Маршруты повторяют устройство боевых страниц обзвона (app/api/routes_obzvon.py):

  GET /{base}       — каркас страницы: БД не трогаем вовсе, отдаётся мгновенно
  GET /{base}/card  — HTML-фрагмент карточки очереди (всё, что требует БД)
  GET /{base}/list  — HTML-фрагмент постраничного списка

где base ∈ {centro1, centro2}; любое другое значение — 404. Фильтры едут в
query-строке и переживают и пролистывание, и смену режима (см. ``_qs``).
Изменяющих маршрутов (upload/delete/clear) здесь НЕТ намеренно: база read-only,
она пересобирается скриптом целиком.

--------------------------------------------------------------------------------
УСТАНОВКА НА СЕРВЕР (точные строки; правится ОДИН боевой файл — app/obzvon.py)
--------------------------------------------------------------------------------
1. Скопировать этот файл в  C:\\seostat\\app\\api\\routes_centro.py

2. Положить собранную базу в  C:\\seostat\\data\\centrifugal.db
   (другой путь — переменной окружения CENTRIFUGAL_DB в .env, например
    CENTRIFUGAL_DB=C:\\seostat\\data\\centrifugal.db).
   Сборщику базы: НЕ включать WAL (оставить journal_mode=DELETE, значение по
   умолчанию) — SQLite не умеет открывать WAL-базу в режиме mode=ro, если
   рядом нет доступного на запись файла -shm.

3. Шаблоны centro.html, centro_card.html, centro_list.html — в
   C:\\seostat\\app\\web\\templates\\  (имена задаются константами TPL_* ниже).

4. В C:\\seostat\\app\\obzvon.py добавить РОВНО две строки:

   а) в блок импортов, рядом со строкой
          from app.api import routes_obzvon
      добавить строку
          from app.api import routes_centro

   б) в create_app(), сразу ПОСЛЕ строки
          app.include_router(routes_obzvon.router, prefix=obz)
      добавить строку
          routes_centro.include_centro(app, obz)

   Больше в obzvon.py/routes_obzvon.py/callbase.py менять НЕЧЕГО: базы kc и meyer
   остаются как есть. Порядок вызова не важен — include_centro сама ставит свои
   маршруты ВЫШЕ catch-all «/{base}» боевого обзвона (иначе тот перехватил бы
   /centro1 и ответил «Неизвестная база обзвона»).

5. (по желанию, правка только шаблона) ссылки на новые базы в общей шапке —
   в app/web/templates/obzvon_base.html сразу после существующего цикла
        {% for slug, name in bases.items() %} … {% endfor %}
   добавить такой же цикл по centro_bases (include_centro кладёт его в
   глобалы Jinja, поэтому он виден и на страницах kc/meyer):
        {% for slug, name in (centro_bases or {}).items() %}
        <li><a href="{{ base_path }}/{{ slug }}"
               {% if slug == base %}aria-current="page"{% endif %}>{{ name }}</a></li>
        {% endfor %}

6. Перезапустить процесс обзвона (служба seostat-obzvon) — основной сервис
   статистики перезапускать не нужно, он про эти базы ничего не знает.

--------------------------------------------------------------------------------
КОНТЕКСТ ШАБЛОНОВ (контракт с версткой)
--------------------------------------------------------------------------------
Общее для всех трёх шаблонов: base, label, hint, bases (kc/meyer — для шапки),
centro_bases, flt, base_path и помощники tel_href, mail_href, fmt_mln,
short_text, split_list.

centro.html (каркас):  msg, filters_active, view ('card'|'list'), page, size,
    skip, frag_url, frag_qs (уже url-энкодена — выводить с |safe),
    card_url, list_url.
centro_card.html:      db_error (''|текст), db_total, stats, total, skip, wrapped,
    company (dict|None), contacts, phones, emails, purchasers, signals,
    okved_all_list, equipment_all_list, sites, regions, okveds, qs_next,
    tel_href, mail_href, fmt_mln, short_text.
centro_list.html:      db_error, db_total, stats, res (PageResult: rows/total/page/
    size/pages/has_prev/has_next/window()/first_no/last_no/offset),
    contacts_by_inn (ИНН -> список контактов), regions, okveds, page_sizes,
    page_url(n), frag_url(n), card_url(row_no), size_url(n), size_frag_url(n),
    tel_href, mail_href, fmt_mln, short_text.

Строки company/contact/signal приходят обычными dict (не sqlite3.Row): так
работают и c.name_short, и c.get(...), и |tojson в шаблоне.
Каждый контакт: {value, kind, person, role, source, source_url, is_purchaser,
in_sales_base, href} — href это готовый tel:/mailto:, source_url — ЖИВАЯ ссылка
на страницу-источник (карточка закупки ЕИС, staff-страница сайта, checko),
уже приведённая к http(s) — подставляется в href как есть; может быть пустой,
тогда показываем просто source. То же и у source_url новостного повода.
Каждый повод: {source, event_type, what, hotness, ts, source_url}.
stats — счётчики базы для бейджей шапки: {total, with_phone, with_email,
with_purchaser, with_signal, in_obzvon}.
db_error непустой = файл базы не найден/не читается; страница при этом всё равно
отдаётся 200 с подсказкой, 500 продажник не увидит.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

# CSRF-защиту не пишем свою: у боевого роутера обзвона уже есть выверенная
# (Sec-Fetch-Site + Origin/Referer), и разрушающие POST обязаны вести себя одинаково
from app.api.routes_obzvon import _same_origin

from app.config import get_settings
from app.services import callbase
from app.web import templates

log = logging.getLogger(__name__)

router = APIRouter(tags=["centro"], include_in_schema=False)

OBZ = get_settings().obzvon_path  # "/obzvon" — префикс ссылок, как у боевых страниц

# slug базы -> название страницы (в callbase.BASES их НЕ добавляем: тогда боевой
# роутер обзвона начал бы принимать centro1/centro2 и искать их в call_company)
BASES = {"centro1": "Центробежные 1", "centro2": "Центробежные 2"}
HINTS = {"centro1": "база продажников", "centro2": "ядро центробежных"}

TPL_PAGE, TPL_CARD, TPL_LIST = "centro.html", "centro_card.html", "centro_list.html"


# ------------------------------------------------------------------ подключение
# Путь по умолчанию — data/centrifugal.db рядом с проектом (на сервере это
# C:\seostat\data\centrifugal.db), как и data/callbase у боевой базы обзвона.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DB = os.path.join(_ROOT, "data", "centrifugal.db")
DB_PATH_ENV = "CENTRIFUGAL_DB"

# Коротко: соединение read-only и на один запрос, ждать блокировку по существу
# нечего (писателей у файла нет). Если скрипт-сборщик в этот момент подменяет
# файл — лучше быстро сказать «база недоступна», чем держать поток продажника.
CONNECT_TIMEOUT = 3.0


class CentroDbUnavailable(RuntimeError):
    """Файл centrifugal.db отсутствует/не читается. Не 500: страницы обзвона
    показывают понятное сообщение, а не трейс."""


def db_path() -> str:
    return (os.environ.get(DB_PATH_ENV) or "").strip() or DEFAULT_DB


def connect() -> sqlite3.Connection:
    """Соединение с centrifugal.db В РЕЖИМЕ ТОЛЬКО ЧТЕНИЯ.

    Именно sqlite3, а не сессия приложения: база автономная, её нет ни в
    metadata приложения, ни в его engine; подмешивать её в SessionLocal значило
    бы либо ATTACH к seo.db (11 ГБ, чужие блокировки), либо второй engine ради
    четырёх SELECT'ов.

    ``file:...?mode=ro`` (URI) — единственный способ гарантировать, что процесс
    панели физически не сможет писать в файл; путь превращаем в URI через
    Path.as_uri(), иначе виндовый «C:\\seostat\\...» в URI не превратить.
    """
    path = db_path()
    if not os.path.exists(path):
        raise CentroDbUnavailable(
            f"База центробежных не найдена: {path}. "
            f"Положите centrifugal.db туда или задайте {DB_PATH_ENV} в .env.")
    try:
        uri = Path(os.path.abspath(path)).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=CONNECT_TIMEOUT)
    except sqlite3.Error as e:
        raise CentroDbUnavailable(f"Не удалось открыть {path}: {e}") from e
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(CONNECT_TIMEOUT * 1000)}")
    except sqlite3.Error:  # noqa: BLE001 — PRAGMA не критичен, timeout уже задан
        pass
    return conn


def connect_rw() -> sqlite3.Connection:
    """Отдельное КОРОТКОЕ соединение на запись — только для отметки «обзвонено».

    Панель читает снимок в mode=ro осознанно (см. connect), и это правило не
    отменяется: пишем мы ровно в одну таблицу hidden, ровно на время одного
    запроса, и сразу закрываемся. Снимок при пересборке эту таблицу НЕ теряет —
    сборщик её не дропает.
    """
    path = db_path()
    if not os.path.exists(path):
        raise CentroDbUnavailable(f"База центробежных не найдена: {path}")
    conn = sqlite3.connect(path, timeout=CONNECT_TIMEOUT)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(CONNECT_TIMEOUT * 1000)}")
        conn.execute("CREATE TABLE IF NOT EXISTS hidden("
                     "  base TEXT NOT NULL, inn TEXT NOT NULL,"
                     "  ts TEXT, note TEXT, PRIMARY KEY(base, inn))")
    except sqlite3.Error:  # noqa: BLE001
        pass
    return conn


def _rows(conn: sqlite3.Connection, sql: str, args=()) -> list[dict]:
    """Строки как обычные dict: sqlite3.Row в Jinja работает только через
    подстрочный доступ, а шаблонам нужны и c.field, и c.get(), и |tojson."""
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ---------------------------------------------------------------------- фильтры
# Имя -> (тип, значение по умолчанию). Все булевы по умолчанию ВЫКЛЮЧЕНЫ (в отличие
# от боевой базы на 161k строк, где «только с телефоном» включён): здесь 555 и 396
# компаний, и часть работы как раз в том, чтобы видеть, у кого контактов нет.
_FILTER_FIELDS = {
    "q": (str, ""), "region": (str, ""), "okved": (str, ""),
    "only_phone": (bool, False),       # есть хотя бы один телефон
    "only_purchaser": (bool, False),   # есть контакт закупщика/снабженца
    # Владелец 28.07: «добавь "есть телефон закупщика", а не контакт».
    # Разница принципиальная для обзвона: почт у закупщиков много, а телефонов
    # мало (по базе продажников 152 почты против 23 телефонов), и звонить можно
    # только по вторым. Отдельный фильтр показывает ровно тех, кому дозвонишься.
    "only_purchaser_phone": (bool, False),
    "only_signal": (bool, False),      # есть новостной повод
    "only_new": (bool, False),         # есть контакт, которого нет у продажников
}


# Что браузер шлёт за отмеченный чекбокс, у которого не проставлен value="1".
# Без этого списка int("on") падал бы в except и молча возвращал ДЕФОЛТ: галочка
# в форме стоит, а отбор не применён — самая незаметная порода ошибки.
_TRUEISH = {"on", "true", "yes", "да"}


def _flt(**raw) -> dict:
    """Сырые значения из query-строки -> нормальный словарь фильтров.
    Числа/булевы принимаем строками: форма-фильтр авто-сабмитится и шлёт пустые
    поля как "", а 422 продажнику показывать нельзя."""
    out = {}
    for name, (typ, default) in _FILTER_FIELDS.items():
        v = raw.get(name)
        if typ is bool:  # None/"" -> дефолт; 0/1/"0"/"1"/"on" -> bool
            if v in (None, ""):
                out[name] = default
            elif isinstance(v, str) and v.strip().lower() in _TRUEISH:
                out[name] = True
            else:
                try:
                    out[name] = bool(int(v))
                except (TypeError, ValueError):
                    out[name] = default
        else:
            out[name] = (v or "").strip()
    return out


def _qs(flt: dict, skip: int = 0, msg: str = "", **extra) -> str:
    """Строка запроса со ВСЕМИ фильтрами — так отбор переживает и пролистывание,
    и переключение «карточка/список». ``extra`` — состояние списка (view/page/size);
    пустые значения в URL не тащим."""
    params: dict = {}
    for name, (typ, _d) in _FILTER_FIELDS.items():
        v = flt[name]
        if typ is bool:
            params[name] = int(v)
        elif v:
            params[name] = v
    if skip:
        params["skip"] = skip
    for name, v in extra.items():
        if v:
            params[name] = v
    if msg:
        params["msg"] = msg
    return urlencode(params)


_VIEWS = ("card", "list")
DEFAULT_VIEW = "card"


def _view(v: str) -> str:
    return v if v in _VIEWS else DEFAULT_VIEW


def _check_base(base: str) -> str:
    if base not in BASES:
        raise HTTPException(404, f"Неизвестная база: {base}")
    return base


# ------------------------------------------------------------------ SQL-фильтры
# Порядок очереди — тот же, что у боевой базы обзвона: сначала общий приоритет,
# при равенстве выручка. ИНН третьим ключом не для смысла, а для СТАБИЛЬНОСТИ:
# список и карточка ходят по одной очереди через LIMIT/OFFSET, и без полного
# детерминированного порядка «строка №7 в списке» и «skip=6 в карточке» могли бы
# указывать на разные компании. NULL в SQLite меньше всего, значит при DESC он
# сам уезжает в конец — компании без выручки не занимают верх очереди.
_ORDER = "rank_metric DESC, revenue_num DESC, inn"


def _where(base: str, q="", region="", okved="", only_phone=False,
           only_purchaser=False, only_purchaser_phone=False,
           only_signal=False, only_new=False) -> tuple[str, list]:
    """Условия отбора одной строкой SQL + параметры. Всё фильтруется в БД: иначе
    постраничность бессмысленна (пришлось бы вычитывать базу целиком, чтобы
    узнать, что попало на страницу)."""
    # Обзвонённые уходят из очереди навсегда: их отмечает оператор кнопкой
    # «Обзвонили — убрать», строки живут в hidden и переживают пересборку снимка.
    cond, args = ["base = ?",
                  "NOT EXISTS (SELECT 1 FROM hidden h WHERE h.base = company.base"
                  " AND h.inn = company.inn)"], [base]
    if region:
        cond.append("region = ?")
        args.append(region)
    if okved:
        cond.append("okved_main = ?")
        args.append(okved)
    if only_phone:
        cond.append("n_phones > 0")
    if only_purchaser:
        cond.append("n_purchaser > 0")
    if only_purchaser_phone:
        # именно ТЕЛЕФОН закупщика: счётчика под это в company нет, поэтому
        # EXISTS по contact с kind='phone' (индекс ix_contact покрывает base+inn)
        cond.append("EXISTS (SELECT 1 FROM contact ct WHERE ct.base = company.base"
                    " AND ct.inn = company.inn AND ct.kind = 'phone'"
                    " AND COALESCE(ct.is_purchaser, 0) = 1)")
    if only_signal:
        cond.append("n_signals > 0")
    if only_new:
        # «Новый контакт» = его не было в базе продажников. Считать это по
        # company нечем (счётчика нет), поэтому EXISTS по contact — на 951
        # компании с индексом ix_contact это доли миллисекунды.
        cond.append("EXISTS (SELECT 1 FROM contact ct WHERE ct.base = company.base"
                    " AND ct.inn = company.inn AND COALESCE(ct.in_sales_base, 0) = 0)")
    q = (q or "").strip()
    if q:
        # search_blob уже casefold-нут сборщиком, поисковую строку приводим так же:
        # регистронезависимого сравнения кириллицы SQLite сам не умеет.
        # instr(), а не LIKE — не нужно экранировать % и _ из пользовательского ввода.
        cond.append("instr(COALESCE(search_blob, ''), ?) > 0")
        args.append(q.casefold())
    return " AND ".join(cond), args


def count_all(conn: sqlite3.Connection, base: str) -> int:
    """Сколько всего компаний в базе (без фильтров) — для шапки страницы."""
    return conn.execute("SELECT COUNT(*) FROM company WHERE base = ?", (base,)).fetchone()[0] or 0


def count_filtered(conn: sqlite3.Connection, base: str, **flt) -> int:
    where, args = _where(base, **flt)
    return conn.execute(f"SELECT COUNT(*) FROM company WHERE {where}", args).fetchone()[0] or 0


def queue_page(conn: sqlite3.Connection, base: str, page: int = 1,
               size: int = callbase.DEFAULT_PAGE_SIZE, **flt) -> callbase.PageResult:
    """Одна страница очереди: LIMIT/OFFSET на стороне БД (тот же приём и та же
    PageResult, что у боевого списка обзвона, — шаблон навигации ведёт себя
    одинаково). Номер страницы приводится в диапазон 1..pages: ?page=99999
    показывает последнюю страницу, а не пустоту и не 500."""
    size = callbase.norm_page_size(size)
    total = count_filtered(conn, base, **flt)
    pages = max(1, -(-total // size))  # округление вверх
    page = min(max(1, callbase.to_int(page, 1)), pages)
    rows: list[dict] = []
    if total:
        where, args = _where(base, **flt)
        rows = _rows(conn, f"SELECT * FROM company WHERE {where} ORDER BY {_ORDER}"
                           f" LIMIT ? OFFSET ?", args + [size, (page - 1) * size])
    return callbase.PageResult(rows=rows, total=total, page=page, size=size, pages=pages)


def queue_pick(conn: sqlite3.Connection, base: str, skip: int = 0,
               **flt) -> tuple[dict | None, int]:
    """Текущая карточка очереди: (компания | None, всего_в_очереди). Одна строка
    через LIMIT 1 OFFSET skip по тому же порядку, что и список."""
    skip = max(0, callbase.to_int(skip, 0))
    total = count_filtered(conn, base, **flt)
    if skip >= total:
        return None, total
    where, args = _where(base, **flt)
    rows = _rows(conn, f"SELECT * FROM company WHERE {where} ORDER BY {_ORDER}"
                       f" LIMIT 1 OFFSET ?", args + [skip])
    return (rows[0] if rows else None), total


# --------------------------------------------------------------------- контакты
# Контакты закупщиков ПЕРВЫМИ — главное требование к карточке: продажнику нужен
# снабженец, а не общий телефон приёмной. Дальше телефоны раньше почты (звонят
# чаще, чем пишут), затем «новые» контакты раньше уже известных продажникам.
_CONTACT_ORDER = ("COALESCE(is_purchaser, 0) DESC,"
                  " CASE kind WHEN 'phone' THEN 0 ELSE 1 END,"
                  " COALESCE(in_sales_base, 0),"
                  " CASE WHEN COALESCE(source_url, '') <> '' THEN 0 ELSE 1 END,"
                  " id")


def tel_href(phone: str) -> str:
    """«+7 (495) 785-94-60» -> «tel:+74957859460» (общий помощник обзвона)."""
    return callbase.tel_href(phone)


def mail_href(email: str) -> str:
    return "mailto:" + (email or "").strip()


_URL_SCHEME = re.compile(r"^[a-z][a-z0-9+\-]*:", re.I)


def src_url(raw) -> str:
    """Ссылка-источник, пригодная для подстановки в href.

    Кликабельный источник у каждого контакта — главное требование владельца,
    поэтому значение из базы приводим к рабочему виду ЗДЕСЬ, а не надеемся на
    шаблон: «checko.ru/company/123» без схемы браузер считает ОТНОСИТЕЛЬНЫМ
    адресом и уводит на /obzvon/centro1/checko.ru/… — ссылка выглядит живой, но
    ведёт в никуда. Чужие схемы (javascript:, data:) в href не пускаем вовсе.
    """
    u = str(raw or "").strip()
    if not u:
        return ""
    if u.lower().startswith(("http://", "https://")):
        return u
    if u.startswith("//"):      # протокол-относительная «//host/path»
        return "http:" + u
    if _URL_SCHEME.match(u):     # javascript:, data:, mailto: — не страница-источник
        return ""
    return "http://" + u        # точка в схеме не встречается, «checko.ru:8080/…» уцелеет


def _contact(row: dict) -> dict:
    """Строка contact -> словарь для шаблона + готовая ссылка для клика."""
    c = dict(row)
    value = (c.get("value") or "").strip()
    c["value"] = value
    c["source"] = (c.get("source") or "").strip()
    c["source_url"] = src_url(c.get("source_url"))
    c["href"] = tel_href(value) if c.get("kind") == "phone" else mail_href(value)
    c["is_purchaser"] = int(c.get("is_purchaser") or 0)
    c["in_sales_base"] = int(c.get("in_sales_base") or 0)
    return c


def contacts(conn: sqlite3.Connection, base: str, inn: str) -> list[dict]:
    """Все контакты компании: закупщики первыми (см. _CONTACT_ORDER)."""
    return [_contact(r) for r in _rows(
        conn, f"SELECT * FROM contact WHERE base = ? AND inn = ? ORDER BY {_CONTACT_ORDER}",
        (base, inn))]


def contacts_by_inn(conn: sqlite3.Connection, base: str,
                    inns: list[str]) -> dict[str, list[dict]]:
    """Контакты сразу для страницы списка — ОДНИМ запросом (иначе 200 строк
    списка дали бы 200 запросов). Порядок внутри компании тот же, что в карточке."""
    inns = [i for i in inns if i]
    if not inns:
        return {}
    holes = ",".join("?" * len(inns))
    out: dict[str, list[dict]] = {}
    for r in _rows(conn, f"SELECT * FROM contact WHERE base = ? AND inn IN ({holes})"
                         f" ORDER BY inn, {_CONTACT_ORDER}", [base] + inns):
        out.setdefault(r["inn"], []).append(_contact(r))
    return out


def signals(conn: sqlite3.Connection, base: str, inn: str) -> list[dict]:
    """Новостные поводы компании: горячие первыми, свежие раньше старых."""
    rows = _rows(conn, "SELECT * FROM signal WHERE base = ? AND inn = ?"
                       " ORDER BY COALESCE(hotness, 0) DESC, COALESCE(ts, '') DESC, id",
                 (base, inn))
    for s in rows:
        s["source_url"] = src_url(s.get("source_url"))
    return rows


# Технические ЛПР — те, ради кого затевался сбор людей. Список держим здесь,
# а не тянем из enrich_db: панель читает СНИМОК и от кода конвейера не зависит.
TECH_ROLES = ("гл.инженер", "гл.энергетик", "гл.механик", "гл.технолог",
              "техдиректор", "нач.производства", "нач.цеха", "АСУ/КИПиА")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """Снимок мог быть собран СТАРЫМ сборщиком, где таблицы людей ещё нет.
    Панель обязана открыться и в этом случае, просто без блока людей."""
    try:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone())
    except sqlite3.Error:
        return False


def persons(conn: sqlite3.Connection, base: str, inn: str) -> list[dict]:
    """Люди компании с должностями — В ТОМ ЧИСЛЕ БЕЗ КОНТАКТОВ.

    Зачем отдельно от contacts: главный инженер без телефона всё равно нужен
    продажнику — он звонит в приёмную и спрашивает человека по имени. В
    contacts такая запись попасть не может, там ключ это сам контакт.
    Порядок: технические ЛПР первыми, затем снабжение, затем остальные;
    внутри группы — сперва те, у кого есть чем связаться.
    """
    if not _table_exists(conn, "person"):
        return []
    rows = _rows(conn, "SELECT * FROM person WHERE base = ? AND inn = ?", (base, inn))
    вес = {"гл.инженер": 0, "гл.энергетик": 1, "гл.механик": 2, "техдиректор": 3,
           "нач.производства": 4, "гл.технолог": 5, "нач.цеха": 6, "АСУ/КИПиА": 7,
           "снабжение/закупки": 8, "директор": 9}
    for p in rows:
        p["source_url"] = src_url(p.get("source_url"))
        p["tel_href"] = tel_href(p.get("phone") or "")
        p["mail_href"] = mail_href(p.get("email") or "")
        p["is_tech"] = 1 if (p.get("role") or "") in TECH_ROLES else 0
    rows.sort(key=lambda p: (вес.get(p.get("role") or "", 50),
                             0 if (p.get("phone") or p.get("email")) else 1,
                             (p.get("person") or "")))
    return rows


def split_list(raw) -> list[str]:
    """« | »- или построчный список из поля выгрузки -> список значений.
    Разделитель у полей okved_all/equipment_all зависит от того, как их собрал
    скрипт, поэтому понимаем оба варианта, а одиночное значение отдаём как есть."""
    s = str(raw or "").strip()
    if not s:
        return []
    for sep in ("|", "\n"):
        if sep in s:
            return [t.strip() for t in s.split(sep) if t.strip()]
    return [s]


# -------------------------------------------------------------------- агрегаты
# Списки для выпадающих фильтров и счётчики шапки. Базы маленькие (555 + 396),
# но запросы одинаковые на каждой перелистнутой странице, поэтому держим их в
# кэше процесса. Версия кэша — mtime+размер файла: скрипт пересобрал
# centrifugal.db -> кэш сам протух, перезапускать службу не нужно.
_agg_cache: dict[tuple, object] = {}


def _db_version() -> tuple:
    try:
        st = os.stat(db_path())
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def _cached_agg(base: str, kind: str, fn):
    key = (_db_version(), base, kind)
    v = _agg_cache.get(key)
    if v is None:
        # Выбрасываем только записи ПРЕЖНИХ версий файла. Полная очистка здесь
        # была бы ошибкой: за один запрос считаются три агрегата (regions, okveds,
        # stats), и каждый следующий вытеснял бы предыдущий — кэш не срабатывал бы
        # НИКОГДА (проверено: 12 пересчётов на 4 запроса вместо 3).
        for k in [k for k in _agg_cache if k[0] != key[0]]:
            _agg_cache.pop(k, None)
        _agg_cache[key] = v = fn()
    return v


def regions(conn: sqlite3.Connection, base: str) -> list[tuple[str, int]]:
    """Регионы базы с количеством компаний, по убыванию (для выпадающего списка)."""
    def _q():
        return [(r, n) for r, n in conn.execute(
            "SELECT region, COUNT(*) FROM company WHERE base = ?"
            " AND COALESCE(region, '') <> '' GROUP BY region"
            " ORDER BY COUNT(*) DESC, region", (base,)).fetchall() if r]
    return _cached_agg(base, "regions", _q)


def okveds(conn: sqlite3.Connection, base: str) -> list[tuple[str, int]]:
    """Основные ОКВЭД базы с количеством — в числовом порядке кода
    (06.10 < 24.1 < 24.10 < 25.11), а не по алфавиту строки."""
    def _q():
        out = [(o, n) for o, n in conn.execute(
            "SELECT okved_main, COUNT(*) FROM company WHERE base = ?"
            " AND COALESCE(okved_main, '') <> '' GROUP BY okved_main", (base,)).fetchall() if o]
        out.sort(key=lambda on: callbase.okved_sort_key(on[0]))
        return out
    return _cached_agg(base, "okveds", _q)


def stats(conn: sqlite3.Connection, base: str) -> dict:
    """Счётчики для бейджей шапки: сколько всего и сколько с чем."""
    def _q():
        r = conn.execute(
            "SELECT COUNT(*),"
            " SUM(CASE WHEN n_phones > 0 THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN n_emails > 0 THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN n_purchaser > 0 THEN 1 ELSE 0 END),"
            # телефон закупщика — отдельный счётчик: именно по нему звонят,
            # а совпадает он с «контактом закупщика» далеко не всегда
            " SUM(CASE WHEN EXISTS (SELECT 1 FROM contact ct"
            "   WHERE ct.base = company.base AND ct.inn = company.inn"
            "   AND ct.kind = 'phone' AND COALESCE(ct.is_purchaser,0) = 1)"
            "  THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN n_signals > 0 THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN in_obzvon = 1 THEN 1 ELSE 0 END)"
            " FROM company WHERE base = ?", (base,)).fetchone()
        keys = ("total", "with_phone", "with_email", "with_purchaser",
                "with_purchaser_phone", "with_signal", "in_obzvon")
        return {k: int(v or 0) for k, v in zip(keys, r)}
    return _cached_agg(base, "stats", _q)


_EMPTY_STATS = {"total": 0, "with_phone": 0, "with_email": 0, "with_purchaser": 0,
                "with_purchaser_phone": 0, "with_signal": 0, "in_obzvon": 0}


def _common(base: str, flt: dict) -> dict:
    """Часть контекста, одинаковая для всех трёх шаблонов."""
    return {
        "base": base, "label": BASES[base], "hint": HINTS.get(base, ""),
        # bases — то, что общий obzvon_base.html рисует ссылками в шапке.
        # Со страниц центробежных отдаём ТОЛЬКО свои базы (владелец 28.07:
        # «им только по центробежным нужен доступ»): лишние вкладки уводят
        # продажника из его очереди. Боевые kc/meyer при этом не меняются —
        # они получают свой контекст из routes_obzvon и открыты по прямым
        # адресам; это отсечение в интерфейсе, а не в правах.
        "bases": BASES, "centro_bases": BASES,
        "flt": flt, "base_path": OBZ,
        "tel_href": tel_href, "mail_href": mail_href,
        "fmt_mln": callbase.fmt_mln, "short_text": callbase.short_text,
        "split_list": split_list,
    }


# --------------------------------------------------------------------- маршруты
# Путь «/centro{n}», а не «/{base}»: боевой обзвон уже держит catch-all «/{base}»,
# и два catch-all в одном приложении разошлись бы по порядку регистрации. Такой
# шаблон совпадает только с /centro*, поэтому /kc и /meyer наши маршруты не
# перехватывают ни при каком порядке подключения. Полный slug собираем обратно
# ниже: base = "centro" + n.


@router.get("/centro{n}")
def centro_page(request: Request, n: str, q: str = "", region: str = "", okved: str = "",
                only_phone: int | None = None, only_purchaser: int | None = None,
                only_purchaser_phone: int | None = None,
                only_signal: int | None = None, only_new: int | None = None,
                f: int = 0, skip: int = 0, msg: str = "",
                view: str = "", page: str = "", size: str = ""):
    """Каркас страницы: БД не трогаем вовсе — отдаётся мгновенно (фильтры из URL),
    карточка/список и списки значений догружаются фрагментом.
    ``f=1`` — признак отправки формы фильтров: тогда отсутствующий чекбокс
    означает «снят» (браузер не шлёт пустые чекбоксы)."""
    base = _check_base(f"centro{n}")
    dflt = 0 if f else None  # None -> значение по умолчанию из _FILTER_FIELDS
    flt = _flt(q=q, region=region, okved=okved,
               only_phone=only_phone if only_phone is not None else dflt,
               only_purchaser=only_purchaser if only_purchaser is not None else dflt,
               only_purchaser_phone=(only_purchaser_phone
                                     if only_purchaser_phone is not None else dflt),
               only_signal=only_signal if only_signal is not None else dflt,
               only_new=only_new if only_new is not None else dflt)
    active = any(flt[name] != default for name, (typ, default) in _FILTER_FIELDS.items())
    view = _view(view)
    size = callbase.norm_page_size(size)
    page_no = max(1, callbase.to_int(page, 1))
    # в адрес фрагмента кладём только то, что нужно ЕМУ: карточке — skip,
    # списку — номер страницы и размер (режим уже зашит в путь)
    if view == "list":
        frag, frag_qs = "list", _qs(flt, 0, page=page_no, size=size)
    else:
        frag, frag_qs = "card", _qs(flt, skip)
    ctx = _common(base, flt)
    ctx.update({
        "skip": skip, "msg": msg, "filters_active": active,
        "view": view, "page": page_no, "size": size,
        "frag_url": f"{OBZ}/{base}/{frag}", "frag_qs": frag_qs,
        # переключатель режимов: фильтры сохраняются, страница сбрасывается
        "card_url": f"{OBZ}/{base}?{_qs(flt, skip, view='card')}",
        "list_url": f"{OBZ}/{base}?{_qs(flt, 0, view='list', size=size)}",
    })
    return templates.TemplateResponse(request, TPL_PAGE, ctx)


@router.get("/centro{n}/card")
def centro_card(request: Request, n: str, q: str = "", region: str = "", okved: str = "",
                only_phone: str = "0", only_purchaser: str = "0",
                only_purchaser_phone: str = "0",
                only_signal: str = "0", only_new: str = "0", skip: int = 0):
    """HTML-фрагмент карточки очереди — ВСЁ, что известно о компании: реквизиты,
    директор и учредители, ОКВЭД, оборудование, финансы, деятельность, все
    контакты с источниками и живыми ссылками, все новостные поводы."""
    base = _check_base(f"centro{n}")
    flt = _flt(q=q, region=region, okved=okved, only_phone=only_phone,
               only_purchaser=only_purchaser,
               only_purchaser_phone=only_purchaser_phone,
               only_signal=only_signal, only_new=only_new)
    ctx = _common(base, flt)
    ctx.update({
        "db_error": "", "db_total": 0, "stats": dict(_EMPTY_STATS),
        "company": None, "total": 0, "skip": skip, "wrapped": False,
        "contacts": [], "phones": [], "emails": [], "purchasers": [], "signals": [],
        "persons": [],
        "okved_all_list": [], "equipment_all_list": [], "sites": [],
        "regions": [], "okveds": [], "qs_next": _qs(flt, skip + 1),
        # та же позиция: удалённая компания уходит из выборки, и на этом же
        # месте оказывается следующая — оператор продолжает без перескоков
        "qs_same": _qs(flt, skip),
    })
    try:
        conn = connect()
    except CentroDbUnavailable as e:
        ctx["db_error"] = str(e)
        return templates.TemplateResponse(request, TPL_CARD, ctx)
    try:
        company, total = queue_pick(conn, base, skip=skip, **flt)
        wrapped = False
        if company is None and skip and total:  # пропустили дальше конца — начинаем сначала
            skip, wrapped = 0, True
            company, total = queue_pick(conn, base, skip=0, **flt)
        cts = contacts(conn, base, company["inn"]) if company else []
        ctx.update({
            "db_total": count_all(conn, base), "stats": stats(conn, base),
            "regions": regions(conn, base), "okveds": okveds(conn, base),
            "company": company, "total": total, "skip": skip, "wrapped": wrapped,
            "contacts": cts,
            "phones": [c for c in cts if c.get("kind") == "phone"],
            "emails": [c for c in cts if c.get("kind") == "email"],
            "purchasers": [c for c in cts if c.get("is_purchaser")],
            "signals": signals(conn, base, company["inn"]) if company else [],
            "persons": persons(conn, base, company["inn"]) if company else [],
            "okved_all_list": split_list(company.get("okved_all")) if company else [],
            "equipment_all_list": split_list(company.get("equipment_all")) if company else [],
            "sites": split_list(company.get("site")) if company else [],
            "qs_next": _qs(flt, skip + 1),  # «Пропустить»
            "qs_same": _qs(flt, skip),   # возврат после «Обзвонили»
        })
    except sqlite3.Error as e:  # битая/недописанная база — сообщение, а не 500
        log.warning("centro: ошибка чтения %s: %s", db_path(), e)
        ctx["db_error"] = f"Не удалось прочитать базу центробежных: {e}"
    finally:
        conn.close()
    return templates.TemplateResponse(request, TPL_CARD, ctx)


@router.get("/centro{n}/list")
def centro_list(request: Request, n: str, q: str = "", region: str = "", okved: str = "",
                only_phone: str = "0", only_purchaser: str = "0",
                only_purchaser_phone: str = "0",
                only_signal: str = "0", only_new: str = "0",
                page: str = "1", size: str = ""):
    """HTML-фрагмент постраничного списка. В БД уходит ровно три вещи: COUNT под
    фильтрами, одна страница строк и контакты этих строк одним запросом."""
    base = _check_base(f"centro{n}")
    flt = _flt(q=q, region=region, okved=okved, only_phone=only_phone,
               only_purchaser=only_purchaser,
               only_purchaser_phone=only_purchaser_phone,
               only_signal=only_signal, only_new=only_new)
    size_n = callbase.norm_page_size(size)

    def page_url(nn: int) -> str:
        """Ссылка на страницу nn с сохранением фильтров и размера страницы.
        Ведёт на полную страницу — навигация работает и без JS."""
        return f"{OBZ}/{base}?{_qs(flt, 0, view='list', page=nn, size=size_n)}"

    def frag_url(nn: int) -> str:
        """Тот же переход, но адрес фрагмента: JS подменяет таблицу на месте."""
        return f"{OBZ}/{base}/list?{_qs(flt, 0, page=nn, size=size_n)}"

    def card_url(row_no: int) -> str:
        """Открыть карточку строки: её позиция в очереди = skip (порядок один
        и тот же, поэтому номер строки списка прямо переводится в skip)."""
        return f"{OBZ}/{base}?{_qs(flt, row_no, view='card')}"

    ctx = _common(base, flt)
    ctx.update({
        "db_error": "", "db_total": 0, "stats": dict(_EMPTY_STATS),
        "res": callbase.PageResult(rows=[], total=0, page=1, size=size_n, pages=1),
        "contacts_by_inn": {}, "regions": [], "okveds": [],
        "page_sizes": callbase.PAGE_SIZES,
        "page_url": page_url, "frag_url": frag_url, "card_url": card_url,
        # смена размера страницы всегда возвращает на первую страницу: иначе
        # «страница 40 по 25» после переключения на 200 уехала бы за конец
        "size_url": lambda k: f"{OBZ}/{base}?{_qs(flt, 0, view='list', size=k)}",
        "size_frag_url": lambda k: f"{OBZ}/{base}/list?{_qs(flt, 0, size=k)}",
    })
    try:
        conn = connect()
    except CentroDbUnavailable as e:
        ctx["db_error"] = str(e)
        return templates.TemplateResponse(request, TPL_LIST, ctx)
    try:
        res = queue_page(conn, base, page=page, size=size_n, **flt)
        ctx.update({
            "res": res,
            "contacts_by_inn": contacts_by_inn(conn, base, [r.get("inn") for r in res.rows]),
            "db_total": count_all(conn, base), "stats": stats(conn, base),
            "regions": regions(conn, base), "okveds": okveds(conn, base),
        })
    except sqlite3.Error as e:
        log.warning("centro: ошибка чтения %s: %s", db_path(), e)
        ctx["db_error"] = f"Не удалось прочитать базу центробежных: {e}"
    finally:
        conn.close()
    return templates.TemplateResponse(request, TPL_LIST, ctx)


@router.post("/centro{n}/done", dependencies=[Depends(_same_origin)])
def centro_done(request: Request, n: str, inn: str = Form(""),
                back: str = Form("")):
    """Отметить компанию обзвонённой — она навсегда уходит из очереди.

    Владелец 28.07: «когда позвонили, её можно удалять, её перенесут в битрикс
    сами». Физически строку НЕ удаляем: пишем отметку в hidden. Причины две —
    снимок пересобирается офлайн-скриптом и настоящее удаление всё равно
    вернулось бы при следующей сборке, а так отметка переживает пересборку; и
    случайный клик можно отменить, данные никуда не делись.
    """
    base = _check_base(f"centro{n}")
    inn = re.sub(r"\D", "", inn or "")
    if not inn:
        raise HTTPException(400, "не передан ИНН")
    conn = connect_rw()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO hidden(base, inn, ts, note) "
            "VALUES (?, ?, datetime('now'), ?)", (base, inn, "обзвонено"))
        conn.commit()
    finally:
        conn.close()
    # назад в очередь с сохранёнными фильтрами; следующая компания встанет первой
    url = back or f"{OBZ}/{base}"
    return RedirectResponse(url=url, status_code=303)


@router.post("/centro{n}/undone", dependencies=[Depends(_same_origin)])
def centro_undone(request: Request, n: str, inn: str = Form(""),
                  back: str = Form("")):
    """Вернуть компанию в очередь — страховка от случайного нажатия."""
    base = _check_base(f"centro{n}")
    inn = re.sub(r"\D", "", inn or "")
    conn = connect_rw()
    try:
        conn.execute("DELETE FROM hidden WHERE base = ? AND inn = ?", (base, inn))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=back or f"{OBZ}/{base}", status_code=303)


# ------------------------------------------------------------------- подключение
def include_centro(app, prefix: str = "") -> None:
    """Подключить страницы центробежных к приложению обзвона.

    Вызывается ОДНОЙ строкой в app/obzvon.py (см. шапку модуля):

        routes_centro.include_centro(app, obz)

    Почему функция, а не голый ``app.include_router``: боевой роутер обзвона
    держит catch-all «/{base}», который совпадает и с «/centro1». Starlette
    отдаёт запрос ПЕРВОМУ совпавшему маршруту и «более точный» не ищет, поэтому
    наши маршруты должны стоять в списке выше. Здесь это делается явно — тогда
    строку установки можно поставить в любое место create_app(), и порядок
    правки боевого файла ни на что не влияет.

    Свои маршруты ставим в САМОЕ НАЧАЛО списка, а не «перед тем, у кого в пути
    {base}»: в свежих версиях FastAPI include_router добавляет не отдельные
    Route с атрибутом .path, а один составной объект, у которого пути не видно —
    искать по нему catch-all бессмысленно. Начало списка безопасно: пути
    «{prefix}/centro…» не пересекаются ни со статикой, ни с /docs, ни с
    остальными страницами обзвона.
    """
    if getattr(app.state, "centro_installed", False):
        return  # идемпотентно: повторный вызов не плодит дублей маршрутов
    routes = app.router.routes
    before = len(routes)
    app.include_router(router, prefix=prefix)
    added = routes[before:]
    del routes[before:]
    routes[0:0] = added
    # чтобы ссылки на новые базы можно было вывести и в шапке страниц kc/meyer
    templates.env.globals["centro_bases"] = BASES
    app.state.centro_installed = True
    path = db_path()
    if not os.path.exists(path):
        log.warning("centro: база %s не найдена — страницы откроются с подсказкой", path)
    log.info("centro: подключены базы %s (БД %s)", ", ".join(BASES), path)
