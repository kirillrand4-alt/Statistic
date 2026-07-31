"""Страницы «Обзвон» — очередь потенциальных клиентов по базам компаний.

Живут в ОТДЕЛЬНОМ приложении ``app.obzvon`` (свой systemd-процесс, свой порт,
свои пароли Basic auth) — продажники не имеют доступа к основному сервису
статистики: у того другой процесс и свой пароль.

GET  /{base}          — каркас страницы (мгновенный, без БД)
GET  /{base}/card     — HTML-фрагмент карточки очереди (всё, что требует БД)
GET  /{base}/list     — HTML-фрагмент постраничного списка очереди
POST /{base}/upload   — загрузка выгрузки Checko (xlsx/tsv/csv)
POST /{base}/delete   — удалить текущую строку и показать следующую
POST /{base}/clear    — очистить базу целиком (для перезаливки)

У страницы два режима (``view`` в URL): «card» — карточка по одной компании
(рабочий режим обзвона, как было) и «list» — постраничный список. Список нужен
на объединённой базе в 161k строк: показывать её целиком нельзя ни базе, ни
браузеру, поэтому строки берутся страницами на стороне БД (callbase.page).
"""
from __future__ import annotations

import base64
import binascii
from urllib.parse import quote, urlencode, urlparse

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.deps import get_db
from app.services import callbase
from app.web import templates

router = APIRouter(tags=["obzvon"], include_in_schema=False)

OBZ = get_settings().obzvon_path  # "/obzvon" — префикс ссылок/редиректов


def _login(request: Request) -> str:
    """Логин из Basic-заголовка (сам заголовок уже проверен middleware'ом)."""
    try:
        scheme, _, cred = (request.headers.get("authorization") or "").partition(" ")
        if scheme.lower() != "basic":
            return ""
        return base64.b64decode(cred.strip()).decode("utf-8").partition(":")[0]
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return ""


def _is_admin(request: Request) -> bool:
    """Загрузка/очистка базы — только для логинов из OBZVON_ADMINS
    (пустая настройка = можно всем, как раньше)."""
    admins = {a.strip() for a in
              get_settings().obzvon_admins.replace(";", ",").split(",") if a.strip()}
    return True if not admins else _login(request) in admins


def _check_base(base: str) -> str:
    if base not in callbase.BASES:
        raise HTTPException(404, f"Неизвестная база обзвона: {base}")
    return base


def _same_origin(request: Request) -> None:
    """CSRF-защита для разрушающих POST: Basic-auth браузер сам дошлёт креды на
    межсайтовую форму, поэтому отклоняем cross-site запросы. Основной сигнал —
    Sec-Fetch-Site (шлют все актуальные браузеры); для древних — Origin/Referer.
    Не-браузерные клиенты (без всех этих заголовков) пропускаем — у них нет
    жертвы-браузера, а значит и CSRF-вектора."""
    sfs = request.headers.get("sec-fetch-site")
    if sfs is not None:
        if sfs not in ("same-origin", "same-site", "none"):
            raise HTTPException(403, "Cross-site запрос отклонён.")
        return
    ref = request.headers.get("origin") or request.headers.get("referer")
    if ref and urlparse(ref).netloc != request.headers.get("host", ""):
        raise HTTPException(403, "Cross-site запрос отклонён.")


# Поля-фильтры очереди: имя -> (тип, значение по умолчанию). Диапазоны — «от/до».
_FILTER_FIELDS = {
    "q": (str, ""), "region": (str, ""), "okved": (str, ""), "equipment": (str, ""),
    "hit_from": (int, 0), "hit_to": (int, 0),
    "rank_from": (float, 0.0), "rank_to": (float, 0.0),
    "rev_from": (float, 0.0), "rev_to": (float, 0.0),
    "only_phone": (bool, True), "active_only": (bool, True),
    "mobile_only": (bool, False),
}


def _flt(**raw) -> dict:
    out = {}
    for name, (typ, default) in _FILTER_FIELDS.items():
        v = raw.get(name)
        if typ is bool:  # None/"" -> дефолт; 0/1/"0"/"1" -> bool
            try:
                out[name] = default if v in (None, "") else bool(int(v))
            except (TypeError, ValueError):
                out[name] = default
        elif typ is str:
            out[name] = (v or "").strip()
        else:  # int/float: ""/None -> дефолт (пустые поля формы не должны падать в 422)
            try:
                out[name] = typ(v) if v else default
            except (TypeError, ValueError):
                out[name] = default
    return out


def _qs(flt: dict, skip: int = 0, msg: str = "", **extra) -> str:
    """Строка запроса со ВСЕМИ фильтрами — так состояние переживает и переход по
    страницам, и переключение режима, и редирект после удаления. ``extra`` —
    состояние списка (view/page/size); пустые значения не тащим в URL."""
    params = {}
    for name, (typ, _d) in _FILTER_FIELDS.items():
        v = flt[name]
        if typ is bool:
            params[name] = int(v)
        elif v:  # пустые строки/нули не тащим в URL
            params[name] = v
    if skip:
        params["skip"] = skip
    for name, v in extra.items():
        if v:
            params[name] = v
    if msg:
        params["msg"] = msg
    return urlencode(params)


# Режимы страницы. По умолчанию — карточка: это привычный рабочий режим обзвона,
# список включается переключателем и запоминается в URL.
_VIEWS = ("card", "list")
DEFAULT_VIEW = "card"


def _view(v: str) -> str:
    return v if v in _VIEWS else DEFAULT_VIEW


@router.get("/")
def obzvon_root():
    first = next(iter(callbase.BASES))
    return RedirectResponse(url=f"{OBZ}/{first}", status_code=307)


@router.get("/{base}")
def obzvon_page(request: Request, base: str, q: str = "", region: str = "",
                okved: str = "", equipment: str = "",
                hit_from: str = "", hit_to: str = "",
                rank_from: str = "", rank_to: str = "",
                rev_from: str = "", rev_to: str = "",
                only_phone: int | None = None, active_only: int | None = None,
                mobile_only: int | None = None, f: int = 0,
                skip: int = 0, msg: str = "",
                view: str = "", page: str = "", size: str = ""):
    # Числовые фильтры принимаем как строки: форма-фильтр авто-сабмитится и шлёт
    # пустые поля как "" — FastAPI отверг бы "" для float/int (422). _flt приводит
    # "" -> дефолт, "5" -> 5. (У /card и /delete таких пустых значений не бывает.)
    """Каркас страницы: БД не трогаем вовсе — отдаётся мгновенно (фильтры из URL),
    карточка компании и списки значений догружаются фрагментом /{base}/card.
    ``f=1`` — признак отправки формы фильтров: тогда отсутствующий чекбокс
    означает «снят» (браузер не шлёт пустые чекбоксы)."""
    base = _check_base(base)
    dflt = 0 if f else None  # None -> значение по умолчанию из _FILTER_FIELDS
    flt = _flt(q=q, region=region, okved=okved, equipment=equipment,
               hit_from=hit_from, hit_to=hit_to, rank_from=rank_from, rank_to=rank_to,
               rev_from=rev_from, rev_to=rev_to,
               only_phone=only_phone if only_phone is not None else dflt,
               active_only=active_only if active_only is not None else dflt,
               mobile_only=mobile_only if mobile_only is not None else dflt)
    active = any(flt[name] != default for name, (typ, default) in _FILTER_FIELDS.items())
    view = _view(view)
    size = callbase.norm_page_size(size)
    page_no = max(1, callbase.to_int(page, 1))
    # фрагмент грузится отдельным запросом; какой именно — решает режим.
    # В адрес фрагмента кладём только то, что нужно ЕМУ: карточке — skip,
    # списку — номер страницы и размер (сам режим уже зашит в путь).
    if view == "list":
        frag, frag_qs = "list", _qs(flt, 0, page=page_no, size=size)
    else:
        frag, frag_qs = "card", _qs(flt, skip)
    return templates.TemplateResponse(request, "obzvon.html", {
        "base": base, "label": callbase.BASES[base], "bases": callbase.BASES,
        "flt": flt, "skip": skip, "msg": msg, "filters_active": active,
        "view": view, "page": page_no, "size": size,
        "frag_url": f"{OBZ}/{base}/{frag}", "frag_qs": frag_qs,
        # ссылки переключателя режимов: фильтры сохраняются, страница сбрасывается
        "card_url": f"{OBZ}/{base}?{_qs(flt, skip, view='card')}",
        "list_url": f"{OBZ}/{base}?{_qs(flt, 0, view='list', size=size)}",
        "base_path": OBZ,  # контекст перекрывает общий Jinja-глобал основного приложения
    })


@router.get("/{base}/card")
def obzvon_card(request: Request, base: str, q: str = "", region: str = "",
                okved: str = "", equipment: str = "",
                hit_from: str = "", hit_to: str = "",
                rank_from: str = "", rank_to: str = "",
                rev_from: str = "", rev_to: str = "",
                only_phone: str = "1", active_only: str = "1", mobile_only: str = "0",
                skip: int = 0, db: Session = Depends(get_db)):
    """HTML-фрагмент с карточкой очереди — всё, что требует БД. Числовые/булевы
    фильтры принимаем строками (как и /{base}) — пустые значения _flt приводит к
    дефолтам, а не роняет в 422 (паритет с эндпоинтом-каркасом)."""
    base = _check_base(base)
    flt = _flt(q=q, region=region, okved=okved, equipment=equipment,
               hit_from=hit_from, hit_to=hit_to, rank_from=rank_from, rank_to=rank_to,
               rev_from=rev_from, rev_to=rev_to, only_phone=only_phone,
               active_only=active_only, mobile_only=mobile_only)
    company, total = callbase.pick(db, base, skip=skip, **flt)
    wrapped = False
    if company is None and skip and total:  # пропустили дальше конца — начинаем сначала
        skip, wrapped = 0, True
        company, total = callbase.pick(db, base, skip=0, **flt)
    return templates.TemplateResponse(request, "obzvon_card.html", {
        "base": base, "label": callbase.BASES[base],
        "flt": flt, "skip": skip, "wrapped": wrapped,
        "regions": callbase.regions(db, base), "okveds": callbase.okveds(db, base),
        "equipments": callbase.equipments(db, base),
        "company": company, "total": total, "db_total": callbase.count(db, base),
        "phones": callbase.split_list(company.phones) if company else [],
        "emails": callbase.split_list(company.emails) if company else [],
        "sites": callbase.split_list(company.sites) if company else [],
        "site_phones": callbase.split_list(company.site_phones) if company else [],
        "site_emails": callbase.split_list(company.site_emails) if company else [],
        "okved_all_list": callbase.split_list(company.okved_all) if company else [],
        "okved_hits_list": callbase.split_list(company.okved_hits) if company else [],
        "equipment_all_list": callbase.split_list(company.equipment_all) if company else [],
        "tel_href": callbase.tel_href,
        "is_admin": _is_admin(request),
        "qs_next": _qs(flt, skip + 1),  # «Пропустить»
        "base_path": OBZ,
    })


@router.get("/{base}/list")
def obzvon_list(request: Request, base: str, q: str = "", region: str = "",
                okved: str = "", equipment: str = "",
                hit_from: str = "", hit_to: str = "",
                rank_from: str = "", rank_to: str = "",
                rev_from: str = "", rev_to: str = "",
                only_phone: str = "1", active_only: str = "1", mobile_only: str = "0",
                page: str = "1", size: str = "", db: Session = Depends(get_db)):
    """HTML-фрагмент постраничного списка очереди.

    В БД уходит ровно две вещи: COUNT под фильтрами (кэшируется) и одна страница
    строк (LIMIT/OFFSET по индексу очереди). Всю базу не читаем ни здесь, ни в
    шаблоне — на 161k строк это и был источник тормозов.

    ``page``/``size`` принимаем строками: ссылки навигации и селект размера
    страницы уходят в тот же авто-сабмитящийся URL, а 422 продажнику показывать
    нельзя — callbase приводит мусор к дефолтам и загоняет номер в диапазон."""
    base = _check_base(base)
    flt = _flt(q=q, region=region, okved=okved, equipment=equipment,
               hit_from=hit_from, hit_to=hit_to, rank_from=rank_from, rank_to=rank_to,
               rev_from=rev_from, rev_to=rev_to, only_phone=only_phone,
               active_only=active_only, mobile_only=mobile_only)
    size_n = callbase.norm_page_size(size)
    res = callbase.page(db, base, page=page, size=size_n, **flt)

    def page_url(n: int) -> str:
        """Ссылка на страницу n с сохранением фильтров и размера страницы.
        Ведёт на полную страницу — навигация работает и без JS."""
        return f"{OBZ}/{base}?{_qs(flt, 0, view='list', page=n, size=size_n)}"

    def frag_url(n: int) -> str:
        """Тот же переход, но адрес фрагмента: JS подменяет таблицу на месте,
        не перезагружая страницу и не теряя развёрнутые фильтры."""
        return f"{OBZ}/{base}/list?{_qs(flt, 0, page=n, size=size_n)}"

    def card_url(row_no: int) -> str:
        """Открыть карточку строки: её позиция в очереди = skip (порядок один
        и тот же, поэтому номер строки списка прямо переводится в skip)."""
        return f"{OBZ}/{base}?{_qs(flt, row_no, view='card')}"

    return templates.TemplateResponse(request, "obzvon_list.html", {
        "base": base, "label": callbase.BASES[base], "flt": flt, "res": res,
        "regions": callbase.regions(db, base), "okveds": callbase.okveds(db, base),
        "equipments": callbase.equipments(db, base),
        "db_total": callbase.count(db, base),
        "page_sizes": callbase.PAGE_SIZES,
        "page_url": page_url, "frag_url": frag_url, "card_url": card_url,
        # смена размера страницы всегда возвращает на первую страницу: иначе
        # «страница 40 по 25» после переключения на 200 уехала бы за конец
        "size_url": lambda n: f"{OBZ}/{base}?{_qs(flt, 0, view='list', size=n)}",
        "size_frag_url": lambda n: f"{OBZ}/{base}/list?{_qs(flt, 0, size=n)}",
        "split_list": callbase.split_list, "tel_href": callbase.tel_href,
        "fmt_mln": callbase.fmt_mln, "short_text": callbase.short_text,
        "is_admin": _is_admin(request),
        "base_path": OBZ,
    })


@router.post("/{base}/upload", dependencies=[Depends(_same_origin)])
async def obzvon_upload(request: Request, base: str, file: UploadFile = File(...),
                        db: Session = Depends(get_db)):
    base = _check_base(base)
    if not _is_admin(request):
        raise HTTPException(403, "Загрузка базы доступна только администратору обзвона.")
    data = await file.read()
    try:
        rows = callbase.parse_upload(file.filename or "", data)
    except Exception as e:  # noqa: BLE001 — битый файл не должен ронять страницу
        return RedirectResponse(
            url=f"{OBZ}/{base}?msg={quote(f'Не удалось разобрать файл: {e}')}",
            status_code=303)
    if not rows:
        msg = "В файле не найден лист/колонки с компаниями (нужны заголовки «ИНН», «Краткое»…)."
        return RedirectResponse(url=f"{OBZ}/{base}?msg={quote(msg)}", status_code=303)
    added, skipped = callbase.import_rows(db, base, rows)
    msg = f"Импортировано {added}, пропущено дублей {skipped}."
    return RedirectResponse(url=f"{OBZ}/{base}?msg={quote(msg)}", status_code=303)


@router.post("/{base}/delete", dependencies=[Depends(_same_origin)])
def obzvon_delete(base: str, company_id: int = Form(...), q: str = Form(""),
                  region: str = Form(""), okved: str = Form(""), equipment: str = Form(""),
                  hit_from: int = Form(0), hit_to: int = Form(0),
                  rank_from: float = Form(0), rank_to: float = Form(0),
                  rev_from: float = Form(0), rev_to: float = Form(0),
                  only_phone: int = Form(0), active_only: int = Form(0),
                  mobile_only: int = Form(0),
                  skip: int = Form(0), db: Session = Depends(get_db)):
    base = _check_base(base)
    ok = callbase.delete_company(db, base, company_id)
    flt = _flt(q=q, region=region, okved=okved, equipment=equipment,
               hit_from=hit_from, hit_to=hit_to, rank_from=rank_from, rank_to=rank_to,
               rev_from=rev_from, rev_to=rev_to, only_phone=only_phone,
               active_only=active_only, mobile_only=mobile_only)
    # skip сохраняем: удалённая строка выпала из очереди, на её месте уже следующая
    msg = "" if ok else "Строка уже удалена."
    return RedirectResponse(url=f"{OBZ}/{base}?{_qs(flt, skip, msg)}", status_code=303)


@router.post("/{base}/clear", dependencies=[Depends(_same_origin)])
def obzvon_clear(request: Request, base: str, db: Session = Depends(get_db)):
    base = _check_base(base)
    if not _is_admin(request):
        raise HTTPException(403, "Очистка базы доступна только администратору обзвона.")
    n = callbase.clear_base(db, base)
    return RedirectResponse(
        url=f"{OBZ}/{base}?msg={quote(f'База очищена (удалено {n}).')}", status_code=303)
