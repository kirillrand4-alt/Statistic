"""Authenticated unified Centro queue (source snapshot + persistent sales state)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections import defaultdict, deque
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.routes_obzvon import _same_origin
from app.api import routes_centro as source
from app.config import get_settings
from app.services import centro_sales as sales
from app.web import templates

router = APIRouter(tags=["centro-sales"], include_in_schema=False)
BP = get_settings().obzvon_path
COOKIE = "centro_session"
MAX_AGE = 8 * 60 * 60
LOGIN_ATTEMPTS: dict[str, deque] = defaultdict(deque)


def _secret() -> bytes:
    return os.getenv("CENTRO_SESSION_SECRET", "").encode()


def _token(username: str, expires: int) -> str:
    body = f"{username}|{expires}"
    signature = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}|{signature}"


def _username(token: str) -> str:
    try:
        username, expires, signature = token.rsplit("|", 2)
        if int(expires) < time.time() or not _secret(): return ""
        expected = hmac.new(_secret(), f"{username}|{expires}".encode(), hashlib.sha256).hexdigest()
        return username if hmac.compare_digest(signature, expected) else ""
    except (ValueError, TypeError):
        return ""


def current_user(request: Request) -> dict:
    username = _username(request.cookies.get(COOKIE, ""))
    if username:
        with sales.connect() as conn:
            row = conn.execute("SELECT id,username,role,is_active FROM users WHERE username=?", (username,)).fetchone()
        if row and row["is_active"]: return dict(row)
    raise HTTPException(401, "Требуется вход", headers={"Location": f"{BP}/centro/login"})


def _source_companies() -> tuple[list[dict], str]:
    with source.connect() as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(company)")}
        rows = [dict(r) for r in conn.execute("SELECT * FROM company")]
    merged = {}
    for row in rows:
        inn = sales.normalize_inn(row.get("inn"))
        if not inn: continue
        if inn not in merged: merged[inn] = row | {"inn": inn}
        else:
            for key, value in row.items():
                if value not in (None, "") and merged[inn].get(key) in (None, ""):
                    merged[inn][key] = value
        c = merged[inn]
        c["has_phone"] = bool(c.get("n_phones") or c.get("telefony_predpriyatiya") or c.get("telefony_iz_bazy"))
        c["has_purchaser"] = bool(c.get("n_purchaser"))
        c["has_tech"] = bool(c.get("tehnicheskih_s_nomerom") or c.get("n_tech"))
        c["has_signal"] = bool(c.get("n_signals") or c.get("novost"))
    return list(merged.values()), ",".join(sorted(columns))


def _contacts(inn: str) -> list[dict]:
    result, by_phone = [], {}
    try:
        with source.connect() as conn:
            if not source._table_exists(conn, "contact"): return []
            rows = [dict(r) for r in conn.execute("SELECT * FROM contact WHERE inn=?", (inn,))]
        for row in rows:
            if row.get("kind") == "phone":
                key = sales.normalize_phone(row.get("value"))
                if not key: continue
            else:
                key = str(row.get("value") or "").strip().casefold()
                if not key: continue
            if key in by_phone:
                old = by_phone[key]
                src = (row.get("source") or "").strip()
                if src and src not in old["sources"]: old["sources"].append(src)
                url = source.src_url(row.get("source_url"))
                if url and url not in old["source_urls"]: old["source_urls"].append(url)
                continue
            item = source._contact(row)
            item["value"] = key if row.get("kind") == "phone" else row.get("value")
            item["sources"] = [row.get("source")] if row.get("source") else []
            item["source_urls"] = [source.src_url(row.get("source_url"))] if source.src_url(row.get("source_url")) else []
            item["is_tech"] = int(bool(row.get("tehLPR") or row.get("is_tech")))
            by_phone[key] = item; result.append(item)
        result.sort(key=lambda c: (not bool(c.get("is_purchaser")), not bool(c.get("is_tech")), c.get("kind") != "phone"))
        return result
    except source.CentroDbUnavailable:
        return []


def _filter(rows: list[dict], request: Request) -> list[dict]:
    q = request.query_params.get("q", "").strip().casefold()
    region = request.query_params.get("region", "")
    status = request.query_params.get("call_status", "")
    phone = request.query_params.get("has_phone", "")
    out = []
    for c in rows:
        blob = " ".join(str(v or "") for v in c.values()).casefold()
        if q and q not in blob: continue
        if region and c.get("region") != region: continue
        if phone == "1" and not c.get("has_phone"): continue
        if status and c.get("call_result", "new") != status: continue
        out.append(c)
    return out


@router.get("/centro/login")
def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "centro_login.html", {"error": error, "base_path": BP})


@router.post("/centro/login", dependencies=[Depends(_same_origin)])
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    key = request.client.host if request.client else "unknown"
    now, attempts = time.time(), LOGIN_ATTEMPTS[key]
    while attempts and attempts[0] < now - 900: attempts.popleft()
    if len(attempts) >= 5: raise HTTPException(429, "Слишком много попыток. Повторите через 15 минут.")
    user = sales.authenticate(username.strip(), password)
    if not user:
        attempts.append(now)
        return RedirectResponse(f"{BP}/centro/login?error=1", 303)
    attempts.clear()
    response = RedirectResponse(f"{BP}/centro", 303)
    response.set_cookie(COOKIE, _token(user["username"], int(now + MAX_AGE)), max_age=MAX_AGE,
                        httponly=True, secure=request.url.scheme == "https", samesite="lax", path="/")
    return response


@router.post("/centro/logout", dependencies=[Depends(_same_origin)])
def logout(request: Request):
    response = RedirectResponse(f"{BP}/centro/login", 303)
    response.delete_cookie(COOKIE, path="/")
    return response


@router.get("/centro")
def centro(request: Request, page: int = 1, size: int = 20, inn: str = "", user=Depends(current_user)):
    try:
        companies, version = _source_companies()
        error = ""
    except source.CentroDbUnavailable as exc:
        companies, version, error = [], "", str(exc)
    with sales.connect() as conn:
        sales_users = [r[0] for r in conn.execute("SELECT username FROM users WHERE role='sales' AND is_active=1 ORDER BY username")]
    if sales_users and companies: sales.assign_new(companies, tuple(sales_users), source_version=version)
    with sales.connect() as conn:
        assignment = {r["inn"]: dict(r) for r in conn.execute("SELECT * FROM company_assignment")}
        states = {(r["inn"],r["username"]):dict(r) for r in conn.execute("SELECT * FROM company_state")}
        comments = [dict(r) for r in conn.execute("SELECT * FROM company_comment ORDER BY created_at DESC LIMIT 100")]
    visible = []
    assigned_user = request.query_params.get("assigned_user", "") if user["role"] == "admin" else user["username"]
    for c in companies:
        a = assignment.get(c["inn"])
        if user["role"] != "admin" and (not a or a["username"] != user["username"]): continue
        if assigned_user and (not a or a["username"] != assigned_user): continue
        c = dict(c); c["assigned_user"] = a["username"] if a else ""
        c.update(states.get((c["inn"], c["assigned_user"]), {})); visible.append(c)
    visible = _filter(visible, request)
    visible.sort(key=lambda c: (c.get("call_result") in {"completed","not_target"}, -(assignment.get(c["inn"],{}).get("assignment_score",0)), c["inn"]))
    size = min(100, max(10, size)); pages = max(1, (len(visible)+size-1)//size); page=min(max(1,page),pages)
    chosen = next((c for c in visible if c["inn"] == sales.normalize_inn(inn)), None)
    if inn and not chosen: raise HTTPException(404, "Компания не найдена или не назначена пользователю")
    if not chosen and visible: chosen = visible[(page-1)*size]
    regions = sorted({str(c.get("region")) for c in companies if c.get("region")})
    query = dict(request.query_params); query.pop("inn", None)
    return templates.TemplateResponse(request, "centro.html", {
        "user":user,"company":chosen,"contacts":_contacts(chosen["inn"]) if chosen else [],
        "comments":[c for c in comments if chosen and c["inn"]==chosen["inn"] and (user["role"]=="admin" or c["username"]==user["username"])],
        "rows":visible[(page-1)*size:page*size],"total":len(visible),"page":page,"pages":pages,
        "regions":regions,"sales_users":sales_users,"error":error,"query_string":urlencode(query),
        "base_path":BP,"call_results":sales.CALL_RESULTS})


@router.post("/centro/save", dependencies=[Depends(_same_origin)])
def save(request: Request, inn: str=Form(...), call_result: str=Form(...), comment: str=Form(""),
         next_contact_at: str=Form(""), return_query: str=Form(""), user=Depends(current_user)):
    try: sales.save_call(user, inn, call_result, next_contact_at, comment)
    except PermissionError: raise HTTPException(404, "Компания не назначена пользователю")
    except ValueError as exc: raise HTTPException(422, str(exc))
    suffix = ("?" + return_query) if return_query else ""
    return RedirectResponse(f"{BP}/centro{suffix}", 303)


@router.post("/centro/comments/{comment_id}", dependencies=[Depends(_same_origin)])
def update_comment(request: Request, comment_id: int, body: str=Form(...), user=Depends(current_user)):
    try: sales.edit_comment(user, comment_id, body)
    except PermissionError: raise HTTPException(403, "Нельзя редактировать чужой комментарий")
    return RedirectResponse(request.headers.get("referer") or f"{BP}/centro",303)


@router.get("/centro/admin")
def admin_page(request: Request, user=Depends(current_user)):
    if user["role"] != "admin": raise HTTPException(403)
    with sales.connect() as conn:
        stats = [dict(r) for r in conn.execute("SELECT a.username,COUNT(*) companies,SUM(a.assignment_score) score,AVG(a.assignment_score) average,"
            "SUM(CASE WHEN s.status='completed' THEN 1 ELSE 0 END) processed FROM company_assignment a LEFT JOIN company_state s ON s.inn=a.inn AND s.username=a.username GROUP BY a.username")]
    return templates.TemplateResponse(request,"centro_admin.html",{"user":user,"stats":stats,"base_path":BP})


@router.post("/centro/reassign", dependencies=[Depends(_same_origin)])
def reassign(request: Request, inn: str=Form(...), username: str=Form(...), user=Depends(current_user)):
    try: sales.reassign(user,inn,username)
    except PermissionError: raise HTTPException(403)
    return RedirectResponse(f"{BP}/centro/admin",303)


@router.get("/centro1")
@router.get("/centro2")
def legacy(request: Request):
    return RedirectResponse(f"{BP}/centro", 307)
