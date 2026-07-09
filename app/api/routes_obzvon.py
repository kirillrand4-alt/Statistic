"""Страницы «Обзвон» — очередь потенциальных клиентов по базам компаний.

Живут в ОТДЕЛЬНОМ приложении ``app.obzvon`` (свой systemd-процесс, свой порт,
свои пароли Basic auth) — продажники не имеют доступа к основному сервису
статистики: у того другой процесс и свой пароль.

GET  /{base}          — карточка текущей компании по фильтрам (+ skip «Пропустить»)
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


def _flt(q="", region="", equipment="", min_priority=0, min_revenue_mln=0.0,
         only_phone=1, active_only=1) -> dict:
    return {"q": q or "", "region": region or "", "equipment": equipment or "",
            "min_priority": int(min_priority or 0),
            "min_revenue_mln": float(min_revenue_mln or 0),
            "only_phone": bool(int(only_phone or 0)), "active_only": bool(int(active_only or 0))}


def _qs(flt: dict, skip: int = 0, msg: str = "") -> str:
    params = {"q": flt["q"], "region": flt["region"], "equipment": flt["equipment"],
              "min_priority": flt["min_priority"] or "",
              "min_revenue_mln": flt["min_revenue_mln"] or "",
              "only_phone": int(flt["only_phone"]), "active_only": int(flt["active_only"])}
    if skip:
        params["skip"] = skip
    if msg:
        params["msg"] = msg
    return urlencode({k: v for k, v in params.items() if v != ""})


@router.get("/")
def obzvon_root():
    first = next(iter(callbase.BASES))
    return RedirectResponse(url=f"{OBZ}/{first}", status_code=307)


@router.get("/{base}")
def obzvon_page(request: Request, base: str, q: str = "", region: str = "",
                equipment: str = "", min_priority: int = 0, min_revenue_mln: float = 0,
                only_phone: int = 1, active_only: int = 1, skip: int = 0,
                msg: str = "", db: Session = Depends(get_db)):
    base = _check_base(base)
    flt = _flt(q, region, equipment, min_priority, min_revenue_mln, only_phone, active_only)
    company, total = callbase.pick(db, base, skip=skip, **flt)
    if company is None and skip and total:  # пропустили дальше конца — вернуться к началу
        return RedirectResponse(url=f"{OBZ}/{base}?{_qs(flt)}", status_code=303)
    return templates.TemplateResponse(request, "obzvon.html", {
        "base": base, "label": callbase.BASES[base], "bases": callbase.BASES,
        "flt": flt, "skip": skip, "msg": msg,
        "company": company, "total": total, "db_total": callbase.count(db, base),
        "phones": callbase.split_list(company.phones) if company else [],
        "emails": callbase.split_list(company.emails) if company else [],
        "sites": callbase.split_list(company.sites) if company else [],
        "tel_href": callbase.tel_href,
        "is_admin": _is_admin(request),
        "base_path": OBZ,  # контекст перекрывает общий Jinja-глобал основного приложения
        "qs_keep": _qs(flt, skip),      # текущее состояние (для форм)
        "qs_next": _qs(flt, skip + 1),  # «Пропустить»
        "qs_first": _qs(flt),           # «Сначала»
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
                  region: str = Form(""), equipment: str = Form(""),
                  min_priority: int = Form(0), min_revenue_mln: float = Form(0),
                  only_phone: int = Form(0), active_only: int = Form(0),
                  skip: int = Form(0), db: Session = Depends(get_db)):
    base = _check_base(base)
    ok = callbase.delete_company(db, base, company_id)
    flt = _flt(q, region, equipment, min_priority, min_revenue_mln, only_phone, active_only)
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
