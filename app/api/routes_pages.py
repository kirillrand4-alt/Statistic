"""Server-rendered HTML pages (Jinja2 + Chart.js)."""
from __future__ import annotations

import secrets
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import CollectionRun, Project, ProjectUrl, Site, Source
from app.deps import get_db, parse_date_range, resolve_period_b
from app.services import growth
from app.services import totals as totals_svc
from app.services.ctr import ctr_for_project
from app.services.loaders import project_page_id_map
from app.services.top_keyword import top_keywords_for_project
from app.utils import normalize_url
from app.web import templates

router = APIRouter(tags=["pages"], include_in_schema=False)

BP = get_settings().base_path  # "" or e.g. "/stat" — for redirect targets


def _public_redirect_uri(request: Request) -> str:
    """The exact OAuth callback URL to register in Google (must match)."""
    base = get_settings().public_base_url.strip().rstrip("/")
    if base:
        return f"{base}{BP}/oauth/callback"
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}{BP}/oauth/callback"


def _sites(db: Session) -> list[Site]:
    return db.execute(select(Site).order_by(Site.id)).scalars().all()


def _resolve_site(db: Session, site_id: int | None) -> Site | None:
    if site_id is not None:
        return db.get(Site, site_id)
    return db.execute(select(Site).order_by(Site.id).limit(1)).scalar_one_or_none()


@router.get("/")
def dashboard(request: Request, site_id: int | None = None, start: str | None = None,
              end: str | None = None, msg: str | None = None, clean: int = 0,
              ratio: float = 10.0, min_impr: int = 100, devices: int = 0,
              db: Session = Depends(get_db)):
    sites = _sites(db)
    site = _resolve_site(db, site_id)
    dr = parse_date_range(start, end)
    ctx = {
        "request": request,
        "msg": msg,
        "sites": sites,
        "site": site,
        "projects": db.execute(select(Project).order_by(Project.id)).scalars().all(),
        "range": dr,
        "clean": bool(clean), "ratio": ratio, "min_impr": min_impr,
        "devices": bool(devices),
        "totals": None,
        "daily": [],
        "top_pages": [],
        "runs": db.execute(
            select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(10)
        ).scalars().all(),
        "gsc_site_url": get_settings().gsc_site_url,
    }
    if site is not None:
        ctx["daily"] = totals_svc.site_daily(db, site.id, dr)
        if devices:
            ctx["top_pages"] = totals_svc.per_page_with_devices(db, site.id, dr)[:20]
        else:
            ctx["top_pages"] = totals_svc.per_page_totals(db, site.id, dr)[:20]
        if clean:
            from app.services.antifraud import clean_values_by_url
            vals = clean_values_by_url(db, site.id, dr, ratio_threshold=ratio, min_impressions=min_impr).values()
            cl = sum(v["clicks"] for v in vals)
            im = sum(v["impressions"] for v in vals)
            pw = sum(v["position"] * v["impressions"] for v in vals)
            ctx["totals"] = {"clicks": cl, "impressions": im,
                             "ctr": (cl / im) if im else 0.0, "position": (pw / im) if im else 0.0}
        else:
            ctx["totals"] = totals_svc.site_totals(db, site.id, dr)
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/upload")
def upload_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "upload.html",
        {
            "request": request,
            "sites": _sites(db),
            "projects": db.execute(select(Project).order_by(Project.id)).scalars().all(),
        },
    )


@router.post("/ui/projects")
def ui_create_project(name: str = Form(...), site_id: int = Form(...),
                      db: Session = Depends(get_db)):
    if db.get(Site, site_id) is None:
        raise HTTPException(404, "site not found")
    project = Project(name=name, site_id=site_id)
    db.add(project)
    db.commit()
    return RedirectResponse(url=f"{BP}/projects/{project.id}", status_code=303)


@router.post("/ui/projects/{project_id}/urls")
async def ui_add_urls(project_id: int, urls_text: str | None = Form(None),
                      file: UploadFile | None = File(None), db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    raw = ""
    if file is not None:
        raw += (await file.read()).decode("utf-8", errors="ignore") + "\n"
    if urls_text:
        raw += urls_text
    existing = {u.normalized_url for u in project.urls}
    for line in raw.replace(",", "\n").splitlines():
        url = line.strip()
        if not url:
            continue
        norm = normalize_url(url)
        if norm and norm not in existing:
            db.add(ProjectUrl(project_id=project.id, url=url, normalized_url=norm))
            existing.add(norm)
    db.commit()
    return RedirectResponse(url=f"{BP}/projects/{project_id}", status_code=303)


@router.post("/ui/collect")
def ui_collect(site_id: int = Form(...), db: Session = Depends(get_db)):
    from app.scheduler.jobs import collect_site, compute_window

    site = db.get(Site, site_id)
    if site is not None:
        dr = compute_window(db, site, date.today(), get_settings().collect_refetch_days)
        try:
            collect_site(db, site, dr)
        except Exception:  # noqa: BLE001 - surfaced via /api/admin/status
            pass
    return RedirectResponse(url=f"{BP}/?site_id={site_id}", status_code=303)


@router.get("/admin")
def admin_page(request: Request, msg: str | None = None, db: Session = Depends(get_db)):
    from sqlalchemy import func

    from app.credentials import get_cred
    from app.db.models import DeviceMetricDaily

    s = get_settings()
    cov = {
        sid: (lo, hi, c)
        for sid, lo, hi, c in db.execute(
            select(
                DeviceMetricDaily.site_id,
                func.min(DeviceMetricDaily.date),
                func.max(DeviceMetricDaily.date),
                func.count(),
            ).group_by(DeviceMetricDaily.site_id)
        ).all()
    }
    device_cov = {
        sid: (f"{lo}..{hi} ({c})" if c else "—") for sid, (lo, hi, c) in cov.items()
    }
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "request": request,
            "msg": msg,
            "gsc_mode": get_cred("gsc_auth_mode"),
            "yandex_connected": bool(get_cred("yandex_wm_token")),
            "oauth_redirect_uri": _public_redirect_uri(request),
            "sites": _sites(db),
            "device_cov": device_cov,
            "sources": db.execute(select(Source).order_by(Source.id)).scalars().all(),
            "runs": db.execute(
                select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(30)
            ).scalars().all(),
            "config": {
                "gsc_auth_mode": s.gsc_auth_mode,
                "gsc_site_url": s.gsc_site_url or "—",
                "enable_scheduler": s.enable_scheduler,
                "collect_cron_hour": s.collect_cron_hour,
                "collect_refetch_days": s.collect_refetch_days,
            },
        },
    )


@router.post("/ui/sites")
def ui_create_site(property_uri: str = Form(...), source_code: str = Form("gsc"),
                   display_name: str | None = Form(None),
                   external_host_id: str | None = Form(None),
                   db: Session = Depends(get_db)):
    source = db.execute(select(Source).where(Source.code == source_code)).scalar_one_or_none()
    if source is None:
        raise HTTPException(400, "unknown source")
    site = Site(
        source_id=source.id,
        property_uri=property_uri.strip(),
        display_name=(display_name or property_uri).strip(),
        external_host_id=(external_host_id.strip() or None) if external_host_id else None,
        enabled=True,
    )
    db.add(site)
    db.commit()
    return RedirectResponse(url=f"{BP}/?site_id={site.id}", status_code=303)


@router.post("/ui/sites/{site_id}/toggle")
def ui_toggle_site(site_id: int, db: Session = Depends(get_db)):
    site = db.get(Site, site_id)
    if site is not None:
        site.enabled = not site.enabled
        db.commit()
    return RedirectResponse(url=f"{BP}/admin", status_code=303)


@router.post("/ui/backfill")
def ui_backfill(site_id: int = Form(...), days: int = Form(90), db: Session = Depends(get_db)):
    from app.scheduler.jobs import run_backfill

    try:
        run_backfill(db, site_id, days)
    except Exception:  # noqa: BLE001 - surfaced via the runs table on /admin
        pass
    return RedirectResponse(url=f"{BP}/admin", status_code=303)


@router.post("/ui/yandex/connect")
def ui_yandex_connect(token: str = Form(...), backfill_days: int = Form(480),
                      db: Session = Depends(get_db)):
    from app.services.connect import connect_yandex

    try:
        result = connect_yandex(db, token, backfill_days, True)
        msg = (
            f"Яндекс подключён. Сайтов: {len(result['site_ids'])}. "
            "Данные загружаются в фоне — обновите дашборд через 1–2 минуты."
        )
        return RedirectResponse(url=f"{BP}/?msg={quote(msg)}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"{BP}/admin?msg={quote('Ошибка Яндекс: ' + str(exc))}", status_code=303)


@router.post("/ui/gsc/connect")
def ui_gsc_connect(mode: str = Form("oauth"), gsc_json: str = Form(""),
                   client_id: str = Form(""), client_secret: str = Form(""),
                   refresh_token: str = Form(""), backfill_days: int = Form(480),
                   db: Session = Depends(get_db)):
    from app.services.connect import connect_gsc_oauth, connect_gsc_service_account

    try:
        if mode == "service_account":
            result = connect_gsc_service_account(db, gsc_json, backfill_days, True)
        else:
            result = connect_gsc_oauth(db, client_id, client_secret, refresh_token, backfill_days, True)
        n = len(result["site_ids"])
        msg = (
            f"Подключено. Сайтов найдено: {n}. Данные загружаются в фоне — "
            "обновите дашборд через 1–2 минуты."
        )
        return RedirectResponse(url=f"{BP}/?msg={quote(msg)}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"{BP}/admin?msg={quote('Ошибка: ' + str(exc))}", status_code=303)


@router.post("/ui/gsc/oauth/start")
def ui_gsc_oauth_start(request: Request, client_id: str = Form(...),
                       client_secret: str = Form(...), backfill_days: int = Form(480)):
    from app.credentials import set_cred
    from app.services.connect import google_auth_url

    client_id = client_id.strip()
    set_cred("gsc_oauth_client_id", client_id)
    set_cred("gsc_oauth_client_secret", client_secret.strip())
    set_cred("gsc_oauth_backfill_days", str(backfill_days))
    state = secrets.token_urlsafe(16)
    set_cred("gsc_oauth_state", state)
    url = google_auth_url(client_id, _public_redirect_uri(request), state)
    return RedirectResponse(url=url, status_code=303)


@router.get("/oauth/callback", name="gsc_oauth_callback")
def gsc_oauth_callback(request: Request, code: str = "", state: str = "", error: str = "",
                       db: Session = Depends(get_db)):
    from app.credentials import get_cred
    from app.services.connect import connect_gsc_oauth, exchange_code_for_refresh_token

    if error:
        return RedirectResponse(url=f"{BP}/admin?msg={quote('Google: ' + error)}", status_code=303)
    if not code or not state or state != get_cred("gsc_oauth_state"):
        return RedirectResponse(
            url=f"{BP}/admin?msg={quote('Авторизация не подтверждена (state).')}", status_code=303
        )
    try:
        client_id = get_cred("gsc_oauth_client_id")
        client_secret = get_cred("gsc_oauth_client_secret")
        refresh_token = exchange_code_for_refresh_token(
            client_id, client_secret, code, _public_redirect_uri(request)
        )
        days = int(get_cred("gsc_oauth_backfill_days", "480") or 480)
        result = connect_gsc_oauth(db, client_id, client_secret, refresh_token, days, True)
        msg = (
            f"Google подключён. Сайтов: {len(result['site_ids'])}. "
            "Данные загружаются в фоне — обновите дашборд через 1–2 минуты."
        )
        return RedirectResponse(url=f"{BP}/?msg={quote(msg)}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"{BP}/admin?msg={quote('Ошибка: ' + str(exc))}", status_code=303)


@router.get("/projects/{project_id}")
def project_page(request: Request, project_id: int, start: str | None = None,
                 end: str | None = None, order_by: str = "clicks",
                 db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    dr = parse_date_range(start, end)
    return templates.TemplateResponse(
        request,
        "project_detail.html",
        {
            "request": request,
            "project": project,
            "site": db.get(Site, project.site_id),
            "range": dr,
            "order_by": order_by,
            "top_keywords": top_keywords_for_project(db, project, dr, order_by),
            "ctr": ctr_for_project(db, project, dr),
            "subset_totals": totals_svc.subset_totals(db, project, dr),
        },
    )


@router.get("/projects/{project_id}/compare")
def project_compare_page(request: Request, project_id: int, metric: str = "clicks",
                         a_start: str | None = None, a_end: str | None = None,
                         b_start: str | None = None, b_end: str | None = None,
                         clean: int = 0, ratio: float = 10.0, min_impr: int = 100,
                         device: str = "all", db: Session = Depends(get_db)):
    from app.services.multi_compare import ENGINE_LABELS, ENGINES, compare_project

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    period_a = parse_date_range(a_start, a_end)
    period_b = resolve_period_b(period_a, b_start, b_end)
    result = compare_project(db, project, metric, period_a, period_b,
                             exclude_bots=bool(clean), ratio=ratio, min_impr=min_impr, device=device)
    return templates.TemplateResponse(
        request,
        "project_compare.html",
        {
            "request": request,
            "project": project,
            "metric": result["metric"],
            "clean": bool(clean), "ratio": ratio, "min_impr": min_impr,
            "device": result["device"],
            "period_a": period_a,
            "period_b": period_b,
            "result": result,
            "engine_list": [(code, ENGINE_LABELS[code]) for code in ENGINES],
        },
    )


@router.get("/compare")
def compare_page(request: Request, site_id: int | None = None, metric: str = "clicks",
                 grouping: str = "site", project_id: int | None = None,
                 a_start: str | None = None, a_end: str | None = None,
                 b_start: str | None = None, b_end: str | None = None,
                 min_impressions: int = 0, clean: int = 0, ratio: float = 10.0,
                 min_impr: int = 100, db: Session = Depends(get_db)):
    sites = _sites(db)
    site = _resolve_site(db, site_id)
    result = None
    period_a = parse_date_range(a_start, a_end)
    period_b = resolve_period_b(period_a, b_start, b_end)
    if site is not None and (a_start or b_start or site_id):
        page_ids = None
        if grouping in ("subset", "page", "query") and project_id:
            project = db.get(Project, project_id)
            if project is not None:
                page_ids = list(project_page_id_map(db, project.site_id, project).values())
        result = growth.compare(
            db, site.id, metric, period_a=period_a, period_b=period_b,
            grouping=grouping, page_ids=page_ids, min_impressions=min_impressions,
            exclude_bots=bool(clean), ratio=ratio, min_impr=min_impr,
        )
    return templates.TemplateResponse(
        request,
        "compare.html",
        {
            "request": request,
            "sites": sites,
            "site": site,
            "projects": db.execute(select(Project).order_by(Project.id)).scalars().all(),
            "metric": metric,
            "grouping": grouping,
            "project_id": project_id,
            "min_impressions": min_impressions,
            "clean": bool(clean), "ratio": ratio, "min_impr": min_impr,
            "period_a": period_a,
            "period_b": period_b,
            "result": result,
        },
    )


def _indexing_ctx(db, request, site_id, a=None, b=None, msg=None, check=None):
    from app.services import indexing

    sites = _sites(db)
    site = _resolve_site(db, site_id)
    snapshots, history, cmp, can_capture = [], [], None, False
    if site is not None:
        can_capture = indexing.supports(site)
        snapshots = indexing.list_snapshots(db, site.id)
        history = indexing.count_history(db, site)
        dates = [s["date"] for s in snapshots]
        da = a or (dates[0] if dates else None)
        db_ = b or (dates[1] if len(dates) > 1 else None)
        if da and db_:
            cmp = indexing.compare_snapshots(db, site.id, date.fromisoformat(da), date.fromisoformat(db_))
            a, b = da, db_
    return {
        "request": request, "msg": msg, "sites": sites, "site": site,
        "snapshots": snapshots, "history": history, "cmp": cmp,
        "a": a, "b": b, "can_capture": can_capture, "check": check,
    }


@router.get("/indexing")
def indexing_page(request: Request, site_id: int | None = None, a: str | None = None,
                  b: str | None = None, msg: str | None = None, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "indexing.html", _indexing_ctx(db, request, site_id, a, b, msg)
    )


@router.post("/ui/indexing/check")
async def ui_indexing_check(request: Request, site_id: int = Form(...),
                            urls_text: str | None = Form(None),
                            file: UploadFile | None = File(None),
                            db: Session = Depends(get_db)):
    from app.services import indexing

    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, "site not found")
    raw = ""
    if file is not None:
        raw += (await file.read()).decode("utf-8", errors="ignore") + "\n"
    if urls_text:
        raw += urls_text
    urls = [line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()]
    check = indexing.check_urls(db, site, urls)
    if check:
        check["input_text"] = "\n".join(r["input"] for r in check["rows"])
    msg = None if check else "Сначала создайте снимок страниц в индексе (кнопка выше) — потом проверяйте список."
    return templates.TemplateResponse(
        request, "indexing.html", _indexing_ctx(db, request, site_id, msg=msg, check=check)
    )


@router.post("/ui/indexing/check/export")
async def ui_indexing_check_export(site_id: int = Form(...), urls_text: str | None = Form(None),
                                   export: str = Form("out:txt"), db: Session = Depends(get_db)):
    """Re-run the check and return matching URLs as a download. `export` = "<only>:<fmt>"
    where only ∈ {out,in,all} and fmt ∈ {txt,csv}."""
    import csv
    import io

    from app.services import indexing

    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, "site not found")
    only, _, fmt = export.partition(":")
    urls = [line.strip() for line in (urls_text or "").replace(",", "\n").splitlines() if line.strip()]
    check = indexing.check_urls(db, site, urls)
    if not check:
        return RedirectResponse(
            url=f"{BP}/indexing?site_id={site_id}&msg={quote('Нет снимка для проверки — создайте снимок.')}",
            status_code=303,
        )
    if only == "in":
        rows = [r for r in check["rows"] if r["in_index"]]
    elif only == "all":
        rows = check["rows"]
    else:
        only, rows = "out", [r for r in check["rows"] if not r["in_index"]]
    tag = {"in": "in_index", "all": "index_check", "out": "not_indexed"}[only]

    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["url", "in_index", "title"])
        for r in rows:
            w.writerow([r["input"], int(r["in_index"]), r["title"] or ""])
        body, media, ext = buf.getvalue(), "text/csv; charset=utf-8", "csv"
    else:
        body = "".join(r["input"] + "\n" for r in rows)
        body, media, ext = body, "text/plain; charset=utf-8", "txt"

    fname = f"{tag}_site{site_id}_{check['captured_on']}.{ext}"
    return Response(content=body, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.post("/ui/indexing/capture")
def ui_indexing_capture(site_id: int = Form(...), db: Session = Depends(get_db)):
    import threading

    from app.services import indexing

    site = db.get(Site, site_id)
    if site is None or not indexing.supports(site):
        return RedirectResponse(
            url=f"{BP}/indexing?site_id={site_id}&msg={quote('Этот источник не отдаёт список страниц в индексе (доступно для Яндекса).')}",
            status_code=303,
        )
    threading.Thread(target=indexing.capture_async, args=(site_id,), daemon=True).start()
    return RedirectResponse(
        url=f"{BP}/indexing?site_id={site_id}&msg={quote('Снимок страниц в индексе создаётся в фоне — обновите через минуту.')}",
        status_code=303,
    )


@router.get("/metrika")
def metrika_page(request: Request, site_id: int | None = None, start: str | None = None,
                 end: str | None = None, msg: str | None = None, db: Session = Depends(get_db)):
    from app.services import visits

    sites = _sites(db)
    site = _resolve_site(db, site_id)
    dr = parse_date_range(start, end)
    summary = visits.summary(db, site.id, dr) if site is not None else None
    return templates.TemplateResponse(
        request,
        "metrika.html",
        {"request": request, "msg": msg, "sites": sites, "site": site,
         "range": dr, "summary": summary},
    )


@router.post("/ui/metrika/upload")
async def ui_metrika_upload(site_id: int = Form(...), file: UploadFile = File(...),
                            db: Session = Depends(get_db)):
    from app.services import visits

    if db.get(Site, site_id) is None:
        raise HTTPException(404, "site not found")
    try:
        n = visits.import_fileobj(db, site_id, file.file, file.filename or "upload")
        msg = f"Загружено визитов: {n}"
    except Exception as exc:  # noqa: BLE001
        msg = f"Ошибка: {exc}"
    finally:
        await file.close()
    return RedirectResponse(url=f"{BP}/metrika?site_id={site_id}&msg={quote(msg)}", status_code=303)


@router.get("/antifraud")
def antifraud_page(request: Request, site_id: int | None = None, start: str | None = None,
                   end: str | None = None, ratio: float = 10.0, min_impr: int = 100,
                   db: Session = Depends(get_db)):
    from app.services.antifraud import analyze

    sites = _sites(db)
    site = _resolve_site(db, site_id)
    dr = parse_date_range(start, end)
    result = analyze(db, site.id, dr, ratio_threshold=ratio, min_impressions=min_impr) if site else None
    return templates.TemplateResponse(
        request,
        "antifraud.html",
        {
            "request": request, "sites": sites, "site": site, "range": dr,
            "ratio": ratio, "min_impr": min_impr, "result": result,
        },
    )
