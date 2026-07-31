"""Authenticated Centro queue with detailed centrifugal-compressor evidence."""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections import defaultdict, deque
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.api.routes_obzvon import _same_origin
from app.config import get_settings
from app.services import centro_catalog as catalog
from app.services import centro_medium
from app.services import centro_sales as sales
from app.web import templates

router = APIRouter(tags=["centro-sales"], include_in_schema=False)
BP = get_settings().obzvon_path
COOKIE = "centro_session"
MAX_AGE = 8 * 60 * 60
LOGIN_ATTEMPTS: dict[str, deque] = defaultdict(deque)

FILTER_ALIASES = {
    "okved": ("okved", "okved_main", "okved_all"),
    "equipment": ("tipy_mashin", "equipment_all", "oborudovanie_po_okved", "oborudovanie"),
    "model": ("marki", "marki_iz_faktov", "models"),
    "medium": ("sreda", "sreda_po_faktam", "medium"),
    "condition": ("sostoyaniy", "sostoyaniya_po_faktam", "pometka", "vyvod_ekspertizy"),
    "legal_status": ("status_egrul", "status"),
}


def _secret() -> bytes:
    return os.getenv("CENTRO_SESSION_SECRET", "").strip().encode("utf-8")


def _require_secret() -> bytes:
    secret = _secret()
    if len(secret) < 32:
        raise HTTPException(503, "CENTRO_SESSION_SECRET не настроен или слишком короткий")
    return secret


def _token(username: str, expires: int) -> str:
    body = f"{username}|{expires}"
    signature = hmac.new(_require_secret(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{body}|{signature}"


def _username(token: str) -> str:
    try:
        username, expires, signature = token.rsplit("|", 2)
        if int(expires) < time.time():
            return ""
        secret = _secret()
        if len(secret) < 32:
            return ""
        expected = hmac.new(
            secret,
            f"{username}|{expires}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return username if hmac.compare_digest(signature, expected) else ""
    except (ValueError, TypeError):
        return ""


def current_user(request: Request) -> dict:
    username = _username(request.cookies.get(COOKIE, ""))
    if username:
        with sales.connect() as conn:
            row = conn.execute(
                "SELECT id,username,role,is_active FROM users WHERE username=?",
                (username,),
            ).fetchone()
        if row and row["is_active"]:
            return dict(row)
    raise HTTPException(
        303,
        "Требуется вход",
        headers={"Location": f"{BP}/centro/login"},
    )


def _source_companies() -> tuple[list[dict], str, dict[str, str]]:
    rows = catalog.list_companies()
    role_phone_inns = catalog.role_phone_inns()
    merged: dict[str, dict] = {}
    for row in rows:
        inn = sales.normalize_inn(row.get("inn"))
        if not inn:
            continue
        current = merged.setdefault(inn, {"inn": inn})
        for key, value in row.items():
            if value not in (None, "") and current.get(key) in (None, ""):
                current[key] = value
        current["has_phone"] = bool(
            current.get("has_phone")
            or current.get("n_phones")
            or current.get("telefony_predpriyatiya")
            or current.get("telefony_iz_bazy")
            or current.get("nomera_bez_vladelca")
        )
        current["has_role_phone"] = inn in role_phone_inns
        current["has_purchaser"] = bool(
            current.get("has_purchaser")
            or current.get("n_purchaser")
            or current.get("zakupshchik")
        )
        current["has_tech"] = bool(
            current.get("has_tech")
            or current.get("n_tech")
            or current.get("tehnicheskih_s_nomerom")
            or current.get("tehnicheskie_lyudi")
        )
        current["has_signal"] = bool(
            current.get("has_signal")
            or current.get("n_signals")
            or current.get("novost")
        )
    return list(merged.values()), catalog.source_version(), catalog.database_info()


def _text(company: dict, aliases: tuple[str, ...]) -> str:
    return " | ".join(str(company.get(name) or "") for name in aliases)


def _number(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        clean = str(value).replace("\u00a0", "").replace(" ", "").replace(",", ".")
        return float(clean)
    except (TypeError, ValueError):
        return None


def _company_number(company: dict, *names: str) -> float | None:
    for name in names:
        value = _number(company.get(name))
        if value is not None:
            return value
    return None


def _matches(rows: list[dict], request: Request) -> list[dict]:
    params = request.query_params
    q = params.get("q", "").strip().casefold()
    exact_inn = q if q.isdigit() and len(q) in {10, 12} else ""
    region = params.get("region", "").strip()
    call_status = params.get("call_status", "").strip()
    assigned_user = params.get("assigned_user", "").strip()
    values = {
        name: params.get(name, "").strip().casefold()
        for name in ("okved", "equipment", "model", "medium", "condition", "legal_status")
    }
    min_revenue = _number(params.get("min_revenue"))
    max_revenue = _number(params.get("max_revenue"))
    min_priority = _number(params.get("min_priority"))
    max_priority = _number(params.get("max_priority"))

    out: list[dict] = []
    for company in rows:
        if exact_inn:
            if str(company.get("inn") or "") == exact_inn:
                out.append(company)
            continue

        blob = str(company.get("search_blob") or "").casefold()
        if not blob:
            blob = " ".join(str(value or "") for value in company.values()).casefold()
        if q and q not in blob:
            continue
        if region and str(company.get("region") or "") != region:
            continue
        if assigned_user and company.get("assigned_user") != assigned_user:
            continue
        if call_status and company.get("call_result", "new") != call_status:
            continue
        if params.get("has_phone") == "1" and not company.get("has_phone"):
            continue
        if params.get("has_role_phone") == "1" and not company.get("has_role_phone"):
            continue
        if params.get("has_purchaser") == "1" and not company.get("has_purchaser"):
            continue
        if params.get("has_tech") == "1" and not company.get("has_tech"):
            continue
        if params.get("has_signal") == "1" and not company.get("has_signal"):
            continue
        if params.get("has_model") == "1" and not _text(company, FILTER_ALIASES["model"]).strip(" |\t"):
            continue
        failed = False
        for name, needle in values.items():
            if needle and needle not in _text(company, FILTER_ALIASES[name]).casefold():
                failed = True
                break
        if failed:
            continue
        revenue = _company_number(company, "vyruchka_rub", "revenue_num", "revenue")
        if min_revenue is not None and (revenue is None or revenue < min_revenue):
            continue
        if max_revenue is not None and (revenue is None or revenue > max_revenue):
            continue
        priority = _company_number(company, "moy_prioritet", "rank_metric", "ball_prioriteta")
        if min_priority is not None and (priority is None or priority < min_priority):
            continue
        if max_priority is not None and (priority is None or priority > max_priority):
            continue
        out.append(company)
    return out


def _choice_values(companies: list[dict], aliases: tuple[str, ...], limit: int = 500) -> list[str]:
    values: set[str] = set()
    for company in companies:
        for alias in aliases:
            values.update(sales.split_values(company.get(alias)))
    return sorted(values, key=str.casefold)[:limit]


def _queue_rank(company: dict) -> tuple:
    result = company.get("call_result") or "new"
    next_contact = company.get("next_contact_at") or "9999-12-31T23:59"
    if result == "new":
        bucket = 0
    elif result in {"callback", "interested", "proposal"}:
        bucket = 1
    elif result in {"no_answer", "contacted", "wrong_number"}:
        bucket = 2
    else:
        bucket = 3
    return (
        bucket,
        next_contact,
        -float(company.get("assignment_score") or 0),
        company.get("inn") or "",
    )


def _fallback_fact(company: dict) -> list[dict]:
    model = company.get("marki") or company.get("marki_iz_faktov") or ""
    evidence = company.get("vyvod_ekspertizy") or company.get("prioritet_pochemu") or ""
    quote = company.get("citaty_dokazatelstv") or ""
    links = sales.split_values(company.get("ssylki_na_istochniki"))
    if not any((model, evidence, quote, links)):
        return []
    item = {
        "status": company.get("pometka") or company.get("sostoyaniya_po_faktam") or "",
        "model": model,
        "equipment_type": company.get("tipy_mashin") or "",
        "medium": company.get("sreda_po_faktam") or company.get("sreda") or "",
        "event_date": company.get("daty_faktov") or company.get("data_zakluchenia") or "",
        "evidence": evidence,
        "quote": quote,
        "source": "сводная база доказательств",
        "source_url": catalog.safe_source_url(links[0]) if links else "",
    }
    return [item] if centro_medium.keep_fact(item) else []


@router.get("/centro/login")
def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse(
        request,
        "centro_login.html",
        {"error": error, "base_path": BP},
    )


@router.post("/centro/login", dependencies=[Depends(_same_origin)])
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    _require_secret()
    key = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = LOGIN_ATTEMPTS[key]
    while attempts and attempts[0] < now - 900:
        attempts.popleft()
    if len(attempts) >= 5:
        raise HTTPException(429, "Слишком много попыток. Повторите через 15 минут.")
    user = sales.authenticate(username.strip(), password)
    if not user:
        attempts.append(now)
        return RedirectResponse(f"{BP}/centro/login?error=1", 303)
    attempts.clear()
    response = RedirectResponse(f"{BP}/centro", 303)
    response.set_cookie(
        COOKIE,
        _token(user["username"], int(now + MAX_AGE)),
        max_age=MAX_AGE,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )
    return response


@router.post("/centro/logout", dependencies=[Depends(_same_origin)])
def logout(request: Request):
    response = RedirectResponse(f"{BP}/centro/login", 303)
    response.delete_cookie(COOKIE, path="/")
    return response


@router.get("/centro")
def centro(
    request: Request,
    page: int = 1,
    size: int = 20,
    inn: str = "",
    user=Depends(current_user),
):
    db_info: dict[str, str] = {}
    try:
        companies, version, db_info = _source_companies()
        error = ""
    except catalog.CentroDbUnavailable as exc:
        companies, version, error = [], "", str(exc)

    with sales.connect() as conn:
        sales_users = [
            row[0]
            for row in conn.execute(
                "SELECT username FROM users WHERE role='sales' AND is_active=1 ORDER BY username"
            )
        ]
    if sales_users and companies:
        sales.assign_new(companies, tuple(sales_users), source_version=version)

    with sales.connect() as conn:
        assignments = {
            row["inn"]: dict(row)
            for row in conn.execute("SELECT * FROM company_assignment")
        }
        states = {
            (row["inn"], row["username"]): dict(row)
            for row in conn.execute("SELECT * FROM company_state")
        }
        comments = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM company_comment ORDER BY created_at DESC LIMIT 1000"
            )
        ]

    visible: list[dict] = []
    requested_owner = (
        request.query_params.get("assigned_user", "").strip()
        if user["role"] == "admin"
        else user["username"]
    )
    for source_company in companies:
        assignment = assignments.get(source_company["inn"])
        if user["role"] != "admin" and (
            not assignment or assignment["username"] != user["username"]
        ):
            continue
        if requested_owner and (
            not assignment or assignment["username"] != requested_owner
        ):
            continue
        company = dict(source_company)
        company["assigned_user"] = assignment["username"] if assignment else ""
        company["assignment_score"] = assignment.get("assignment_score", 0) if assignment else 0
        if assignment:
            company.update(states.get((company["inn"], assignment["username"]), {}))
        visible.append(company)

    visible = _matches(visible, request)
    visible.sort(key=_queue_rank)
    size = min(100, max(10, size))
    pages = max(1, (len(visible) + size - 1) // size)
    page = min(max(1, page), pages)
    normalized_inn = sales.normalize_inn(inn)
    chosen = next((company for company in visible if company["inn"] == normalized_inn), None)
    if inn and not chosen:
        raise HTTPException(404, "Компания не найдена или не назначена пользователю")
    if not chosen and visible:
        chosen = visible[(page - 1) * size]

    query = dict(request.query_params)
    query.pop("inn", None)
    query.pop("page", None)
    filter_query = urlencode(query)
    all_regions = sorted(
        {str(company.get("region")) for company in companies if company.get("region")},
        key=str.casefold,
    )
    choices = {
        name: _choice_values(companies, aliases)
        for name, aliases in FILTER_ALIASES.items()
    }

    selected_comments = [
        comment
        for comment in comments
        if chosen
        and comment["inn"] == chosen["inn"]
        and (user["role"] == "admin" or comment["username"] == user["username"])
    ]
    contact_rows: list[dict] = []
    role_contacts: list[dict] = []
    unassigned_contacts: list[dict] = []
    facts: list[dict] = []
    news: list[dict] = []
    people: list[dict] = []
    company_sources: list[dict] = []
    if chosen and not error:
        try:
            contact_rows = catalog.contacts(chosen["inn"])
            role_contacts = [item for item in contact_rows if item.get("has_role")]
            unassigned_contacts = [item for item in contact_rows if not item.get("has_role")]
            facts = catalog.facts(chosen["inn"])
            news = catalog.signals(chosen["inn"])
            people = catalog.persons(chosen["inn"])
            company_sources = catalog.company_sources(chosen["inn"])
        except catalog.CentroDbUnavailable:
            pass
        if not facts:
            facts = _fallback_fact(chosen)
        if not news and (chosen.get("novost") or chosen.get("novost_ssylka")):
            news = [
                {
                    "title": chosen.get("novost") or "Новостной повод",
                    "event_type": "новость предприятия",
                    "event_date": "",
                    "quote": "",
                    "source": "новостной поиск",
                    "source_url": catalog.safe_source_url(chosen.get("novost_ssylka")),
                }
            ]

    return templates.TemplateResponse(
        request,
        "centro.html",
        {
            "user": user,
            "company": chosen,
            "contacts": contact_rows,
            "role_contacts": role_contacts,
            "unassigned_contacts": unassigned_contacts,
            "facts": facts,
            "news": news,
            "people": people,
            "company_sources": company_sources,
            "comments": selected_comments,
            "rows": visible[(page - 1) * size : page * size],
            "total": len(visible),
            "page": page,
            "pages": pages,
            "size": size,
            "regions": all_regions,
            "choices": choices,
            "sales_users": sales_users,
            "error": error,
            "query_string": filter_query,
            "base_path": BP,
            "call_results": sales.CALL_RESULT_LABELS,
            "db_info": db_info,
        },
    )


@router.post("/centro/save", dependencies=[Depends(_same_origin)])
def save(
    request: Request,
    inn: str = Form(...),
    call_result: str = Form(...),
    comment: str = Form(""),
    next_contact_at: str = Form(""),
    return_query: str = Form(""),
    user=Depends(current_user),
):
    try:
        sales.save_call(user, inn, call_result, next_contact_at, comment)
    except PermissionError:
        raise HTTPException(404, "Компания не назначена пользователю")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    suffix = f"?{return_query}" if return_query else ""
    return RedirectResponse(f"{BP}/centro{suffix}", 303)


@router.post("/centro/comments/{comment_id}", dependencies=[Depends(_same_origin)])
def update_comment(
    request: Request,
    comment_id: int,
    body: str = Form(...),
    user=Depends(current_user),
):
    try:
        sales.edit_comment(user, comment_id, body)
    except PermissionError:
        raise HTTPException(403, "Нельзя редактировать чужой комментарий")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return RedirectResponse(request.headers.get("referer") or f"{BP}/centro", 303)


@router.get("/centro/admin")
def admin_page(request: Request, user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    with sales.connect() as conn:
        stats = [
            dict(row)
            for row in conn.execute(
                "SELECT a.username, COUNT(*) companies, "
                "SUM(a.assignment_score) score, AVG(a.assignment_score) average, "
                "SUM(CASE WHEN COALESCE(s.call_result,'new') <> 'new' THEN 1 ELSE 0 END) processed "
                "FROM company_assignment a "
                "LEFT JOIN company_state s ON s.inn=a.inn AND s.username=a.username "
                "GROUP BY a.username ORDER BY a.username"
            )
        ]
        users = [
            row[0]
            for row in conn.execute(
                "SELECT username FROM users WHERE role='sales' AND is_active=1 ORDER BY username"
            )
        ]
    try:
        db_info = catalog.database_info()
    except catalog.CentroDbUnavailable:
        db_info = {}
    return templates.TemplateResponse(
        request,
        "centro_admin.html",
        {
            "user": user,
            "stats": stats,
            "sales_users": users,
            "base_path": BP,
            "db_info": db_info,
        },
    )


@router.post("/centro/reassign", dependencies=[Depends(_same_origin)])
def reassign(
    request: Request,
    inn: str = Form(...),
    username: str = Form(...),
    user=Depends(current_user),
):
    try:
        sales.reassign(user, inn, username)
    except PermissionError:
        raise HTTPException(403)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return RedirectResponse(f"{BP}/centro/admin", 303)


@router.get("/centro1")
@router.get("/centro2")
def legacy(request: Request):
    return RedirectResponse(f"{BP}/centro", 307)
