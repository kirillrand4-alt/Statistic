"""Страницы «Обзвон» — очередь потенциальных клиентов по базам компаний.

GET /calls/{base} — карточка текущей компании по фильтрам (+ skip «Пропустить»);
POST /ui/calls/{base}/upload — загрузка выгрузки Checko (xlsx/tsv/csv);
POST /ui/calls/{base}/delete — удалить текущую строку и показать следующую;
POST /ui/calls/{base}/clear — очистить базу целиком (для перезаливки).
"""
from __future__ import annotations

from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.deps import get_db
from app.services import callbase
from app.web import templates

router = APIRouter(tags=["calls"], include_in_schema=False)

BP = get_settings().base_path


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


@router.get("/calls/{base}")
def calls_page(request: Request, base: str, q: str = "", region: str = "",
               equipment: str = "", min_priority: int = 0, min_revenue_mln: float = 0,
               only_phone: int = 1, active_only: int = 1, skip: int = 0,
               msg: str = "", db: Session = Depends(get_db)):
    base = _check_base(base)
    flt = _flt(q, region, equipment, min_priority, min_revenue_mln, only_phone, active_only)
    company, total = callbase.pick(db, base, skip=skip, **flt)
    if company is None and skip and total:  # пропустили дальше конца — вернуться к началу
        return RedirectResponse(url=f"{BP}/calls/{base}?{_qs(flt)}", status_code=303)
    return templates.TemplateResponse(request, "calls.html", {
        "base": base, "label": callbase.BASES[base], "bases": callbase.BASES,
        "flt": flt, "skip": skip, "msg": msg,
        "company": company, "total": total, "db_total": callbase.count(db, base),
        "phones": callbase.split_list(company.phones) if company else [],
        "emails": callbase.split_list(company.emails) if company else [],
        "sites": callbase.split_list(company.sites) if company else [],
        "tel_href": callbase.tel_href,
        "qs_keep": _qs(flt, skip),      # текущее состояние (для форм)
        "qs_next": _qs(flt, skip + 1),  # «Пропустить»
        "qs_first": _qs(flt),           # «Сначала»
    })


@router.post("/ui/calls/{base}/upload")
async def ui_calls_upload(base: str, file: UploadFile = File(...),
                          db: Session = Depends(get_db)):
    base = _check_base(base)
    data = await file.read()
    try:
        rows = callbase.parse_upload(file.filename or "", data)
    except Exception as e:  # noqa: BLE001 — битый файл не должен ронять страницу
        return RedirectResponse(
            url=f"{BP}/calls/{base}?msg={quote(f'Не удалось разобрать файл: {e}')}",
            status_code=303)
    if not rows:
        msg = "В файле не найден лист/колонки с компаниями (нужны заголовки «ИНН», «Краткое»…)."
        return RedirectResponse(url=f"{BP}/calls/{base}?msg={quote(msg)}", status_code=303)
    added, skipped = callbase.import_rows(db, base, rows)
    msg = f"Импортировано {added}, пропущено дублей {skipped}."
    return RedirectResponse(url=f"{BP}/calls/{base}?msg={quote(msg)}", status_code=303)


@router.post("/ui/calls/{base}/delete")
def ui_calls_delete(base: str, company_id: int = Form(...), q: str = Form(""),
                    region: str = Form(""), equipment: str = Form(""),
                    min_priority: int = Form(0), min_revenue_mln: float = Form(0),
                    only_phone: int = Form(0), active_only: int = Form(0),
                    skip: int = Form(0), db: Session = Depends(get_db)):
    base = _check_base(base)
    ok = callbase.delete_company(db, base, company_id)
    flt = _flt(q, region, equipment, min_priority, min_revenue_mln, only_phone, active_only)
    # skip сохраняем: удалённая строка выпала из очереди, на её месте уже следующая
    msg = "" if ok else "Строка уже удалена."
    return RedirectResponse(url=f"{BP}/calls/{base}?{_qs(flt, skip, msg)}", status_code=303)


@router.post("/ui/calls/{base}/clear")
def ui_calls_clear(base: str, db: Session = Depends(get_db)):
    base = _check_base(base)
    n = callbase.clear_base(db, base)
    return RedirectResponse(
        url=f"{BP}/calls/{base}?msg={quote(f'База очищена (удалено {n}).')}", status_code=303)
