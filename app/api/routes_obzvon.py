"""Страницы «Обзвон» — очередь потенциальных клиентов по базам компаний.

Живут в ОТДЕЛЬНОМ приложении ``app.obzvon`` (свой systemd-процесс, свой порт,
свои пароли Basic auth) — продажники не имеют доступа к основному сервису
статистики: у того другой процесс и свой пароль.

GET  /{base}          — каркас страницы (мгновенный, без БД)
GET  /{base}/card     — HTML-фрагмент карточки очереди (всё, что требует БД)
POST /{base}/upload   — загрузка выгрузки Checko (xlsx/tsv/csv)
POST /{base}/delete   — удалить текущую строку и показать следующую
POST /{base}/clear    — очистить базу целиком (для перезаливки)
"""
from __future__ import annotations

import base64
import binascii
from urllib.parse import quote, urlencode

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
        if typ is bool:
            out[name] = bool(int(v)) if v is not None else default
        elif typ is str:
            out[name] = (v or "").strip()
        else:
            try:
                out[name] = typ(v) if v else default
            except (TypeError, ValueError):
                out[name] = default
    return out


def _qs(flt: dict, skip: int = 0, msg: str = "") -> str:
    params = {}
    for name, (typ, _d) in _FILTER_FIELDS.items():
        v = flt[name]
        if typ is bool:
            params[name] = int(v)
        elif v:  # пустые строки/нули не тащим в URL
            params[name] = v
    if skip:
        params["skip"] = skip
    if msg:
        params["msg"] = msg
    return urlencode(params)


@router.get("/")
def obzvon_root():
    first = next(iter(callbase.BASES))
    return RedirectResponse(url=f"{OBZ}/{first}", status_code=307)


@router.get("/{base}")
def obzvon_page(request: Request, base: str, q: str = "", region: str = "",
                okved: str = "", equipment: str = "",
                hit_from: int = 0, hit_to: int = 0,
                rank_from: float = 0, rank_to: float = 0,
                rev_from: float = 0, rev_to: float = 0,
                only_phone: int | None = None, active_only: int | None = None,
                mobile_only: int | None = None, f: int = 0,
                skip: int = 0, msg: str = ""):
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
    return templates.TemplateResponse(request, "obzvon.html", {
        "base": base, "label": callbase.BASES[base], "bases": callbase.BASES,
        "flt": flt, "skip": skip, "msg": msg,
        "card_qs": _qs(flt, skip),
        "base_path": OBZ,  # контекст перекрывает общий Jinja-глобал основного приложения
    })


@router.get("/{base}/card")
def obzvon_card(request: Request, base: str, q: str = "", region: str = "",
                okved: str = "", equipment: str = "",
                hit_from: int = 0, hit_to: int = 0,
                rank_from: float = 0, rank_to: float = 0,
                rev_from: float = 0, rev_to: float = 0,
                only_phone: int = 1, active_only: int = 1, mobile_only: int = 0,
                skip: int = 0, db: Session = Depends(get_db)):
    """HTML-фрагмент с карточкой очереди — всё, что требует БД."""
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


@router.post("/{base}/upload")
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


@router.post("/{base}/delete")
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


@router.post("/{base}/clear")
def obzvon_clear(request: Request, base: str, db: Session = Depends(get_db)):
    base = _check_base(base)
    if not _is_admin(request):
        raise HTTPException(403, "Очистка базы доступна только администратору обзвона.")
    n = callbase.clear_base(db, base)
    return RedirectResponse(
        url=f"{OBZ}/{base}?msg={quote(f'База очищена (удалено {n}).')}", status_code=303)
