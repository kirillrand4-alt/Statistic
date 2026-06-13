"""Server-rendered HTML pages (Jinja2 + Chart.js)."""
from __future__ import annotations

import secrets
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import CollectionRun, Project, ProjectUrl, Site, Source
from app.deps import get_db, parse_date_range, resolve_period_b
from app.services import growth
from app.services import totals as totals_svc
from app.services.ctr import ctr_for_project
from app.services.loaders import project_page_ids
from app.services.top_keyword import top_keywords_for_project
from app.utils import domain_of, normalize_url
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


def _same_domain_ids(db: Session, site: Site) -> list[int]:
    """site_ids of the SAME source sharing the bare domain — e.g. a GSC domain
    property (sc-domain:) and its https:// URL-prefix property of one site."""
    d = domain_of(site.property_uri)
    rows = db.execute(
        select(Site.id, Site.property_uri).where(Site.source_id == site.source_id)
    ).all()
    ids = [sid for sid, uri in rows if d and domain_of(uri) == d]
    return ids or [site.id]


# Search engines shown as pickable channels on the dashboard (Metrica is visits,
# not search clicks, so it's excluded here).
SEARCH_ENGINES = ("gsc", "yandex_webmaster")


def _domains(db: Session) -> list[dict]:
    """Distinct bare domains across GSC/Yandex sites + which engines each has.
    https:// and sc-domain: of one domain collapse into a single entry."""
    rows = db.execute(
        select(Source.code, Site.property_uri).join(Site, Site.source_id == Source.id)
    ).all()
    m: dict[str, set] = {}
    for code, uri in rows:
        if code in SEARCH_ENGINES:
            d = domain_of(uri)
            if d:
                m.setdefault(d, set()).add(code)
    return [{"domain": d, "engines": sorted(m[d])} for d in sorted(m)]


def _domain_engine_ids(db: Session, domain: str, engine_codes) -> dict[str, list[int]]:
    """{engine_code: [site_ids]} for one domain — every same-host property of that
    engine (so https:// + sc-domain: are merged downstream)."""
    rows = db.execute(
        select(Site.id, Site.property_uri, Source.code).join(Source, Site.source_id == Source.id)
    ).all()
    out: dict[str, list[int]] = {}
    for sid, uri, code in rows:
        if code in engine_codes and domain_of(uri) == domain:
            out.setdefault(code, []).append(sid)
    return out


def _clean_combined(db, engine_ids, dr, ratio, min_impr) -> dict:
    """Bot-cleaned totals: de-dup per-URL within an engine (max impressions),
    then sum across engines."""
    from app.services.antifraud import clean_values_by_url

    by: dict[str, dict] = {}
    for ids in engine_ids.values():
        merged: dict[str, dict] = {}
        for sid in ids:
            for url, v in clean_values_by_url(
                db, sid, dr, ratio_threshold=ratio, min_impressions=min_impr
            ).items():
                cur = merged.get(url)
                if cur is None or v["impressions"] > cur["impressions"]:
                    merged[url] = v
        for url, v in merged.items():
            b = by.setdefault(url, {"clicks": 0, "impressions": 0, "pw": 0.0})
            b["clicks"] += v["clicks"]
            b["impressions"] += v["impressions"]
            b["pw"] += v["position"] * v["impressions"]
    cl = sum(b["clicks"] for b in by.values())
    im = sum(b["impressions"] for b in by.values())
    pw = sum(b["pw"] for b in by.values())
    return {"clicks": cl, "impressions": im, "ctr": (cl / im) if im else 0.0,
            "position": (pw / im) if im else 0.0}


@router.get("/")
def dashboard(request: Request, domain: str | None = None,
              engines: list[str] | None = Query(None), start: str | None = None,
              end: str | None = None, msg: str | None = None, clean: int = 0,
              ratio: float = 10.0, min_impr: int = 100, devices: int = 0,
              gran: str = "day", db: Session = Depends(get_db)):
    domains = _domains(db)
    cur = domain if domain and any(d["domain"] == domain for d in domains) \
        else (domains[0]["domain"] if domains else None)
    avail = next((d["engines"] for d in domains if d["domain"] == cur), [])
    sel = [e for e in (engines or avail) if e in avail] or avail  # default: all the domain has
    dr = parse_date_range(start, end)
    ctx = {
        "request": request, "msg": msg, "domains": domains, "cur_domain": cur,
        "engines": sel, "range": dr, "gran": gran,
        "projects": db.execute(select(Project).order_by(Project.id)).scalars().all(),
        "clean": bool(clean), "ratio": ratio, "min_impr": min_impr, "devices": bool(devices),
        "totals": None, "daily": [], "top_pages": [], "export_sites": [], "site_ids": [],
        "runs": db.execute(
            select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(10)
        ).scalars().all(),
        "gsc_site_url": get_settings().gsc_site_url,
    }
    if cur and sel:
        engine_ids = _domain_engine_ids(db, cur, sel)  # {code: [site_ids]}
        parts = list(engine_ids.values())
        labels = {"gsc": "Google", "yandex_webmaster": "Яндекс"}
        ctx["export_sites"] = [{"id": sid, "label": labels.get(code, code)}
                               for code, ids in engine_ids.items() for sid in ids]
        ctx["site_ids"] = [s["id"] for s in ctx["export_sites"]]
        daily = totals_svc.combine_daily([totals_svc.site_daily(db, ids, dr) for ids in parts])
        ctx["daily"] = totals_svc.bucket_series(daily, gran)
        if devices:
            ctx["top_pages"] = totals_svc.combine_pages_devices(
                [totals_svc.per_page_with_devices(db, ids, dr) for ids in parts])
        else:
            ctx["top_pages"] = totals_svc.combine_pages(
                [totals_svc.per_page_totals(db, ids, dr) for ids in parts])
        if clean:
            ctx["totals"] = _clean_combined(db, engine_ids, dr, ratio, min_impr)
        else:
            ctx["totals"] = totals_svc.combine_totals(
                [totals_svc.site_totals(db, ids, dr) for ids in parts])
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
def ui_collect(site_id: list[int] = Form(...), domain: str = Form(""),
               engines: list[str] = Form([]), db: Session = Depends(get_db)):
    from app.scheduler.jobs import collect_site, compute_window

    for sid in site_id:  # a domain can span several properties / engines
        site = db.get(Site, sid)
        if site is None:
            continue
        dr = compute_window(db, site, date.today(), get_settings().collect_refetch_days)
        try:
            collect_site(db, site, dr)
        except Exception:  # noqa: BLE001 - surfaced via /api/admin/status
            pass
    qs = f"?domain={quote(domain)}" if domain else ""
    for e in engines:
        qs += ("&" if qs else "?") + f"engines={quote(e)}"
    return RedirectResponse(url=f"{BP}/{qs}", status_code=303)


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
                 end: str | None = None, order_by: str = "clicks", merge: int = 0,
                 gran: str = "day", brand: list[str] | None = Query(None),
                 msg: str | None = None, db: Session = Depends(get_db)):
    from app.services import brands as brands_svc
    from app.services.goals import page_key

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    dr = parse_date_range(start, end)
    site = db.get(Site, project.site_id)
    site_ids, merged = None, None
    if merge and site is not None:
        ids = _same_domain_ids(db, site)
        if len(ids) > 1:
            site_ids = ids
            merged = {"count": len(ids), "domain": domain_of(site.property_uri)}

    # Brand filter: restrict the project's URLs to the chosen brands of its domain
    domain = domain_of(site.property_uri) if site else ""
    brand_list = brands_svc.list_brands(db, domain) if domain else []
    sel_brands = [b for b in (brand or []) if any(x["brand"] == b for x in brand_list)]
    only_norms, brand_keys = None, None
    if sel_brands:
        brand_keys = brands_svc.brand_url_keys(db, domain, sel_brands)
        only_norms = {u.normalized_url for u in project.urls if page_key(u.url) in brand_keys}

    return templates.TemplateResponse(
        request,
        "project_detail.html",
        {
            "request": request,
            "project": project,
            "site": site,
            "range": dr, "msg": msg,
            "order_by": order_by,
            "merge": bool(merge), "merged": merged, "gran": gran,
            "brands": brand_list, "sel_brands": sel_brands,
            "top_keywords": top_keywords_for_project(db, project, dr, order_by,
                                                     site_ids=site_ids, only_norms=only_norms),
            "ctr": ctr_for_project(db, project, dr, site_ids=site_ids, only_norms=only_norms),
            "subset_totals": totals_svc.subset_totals(db, project, dr, site_ids=site_ids,
                                                      only_norms=only_norms),
            "daily": totals_svc.bucket_series(
                totals_svc.subset_daily(db, project, dr, site_ids=site_ids,
                                        only_norms=only_norms), gran),
            "goals": _project_goals(db, project, site, dr, gran, brand_keys=brand_keys),
        },
    )


@router.post("/ui/projects/{project_id}/brands")
async def ui_project_brands(project_id: int, file: UploadFile = File(...),
                            db: Session = Depends(get_db)):
    from app.services import brands as brands_svc

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    try:
        res = brands_svc.import_brand_csv(db, await file.read())
        msg = f"Загружено брендов: {res['brands']}, URL: {res['rows']}"
    except Exception as exc:  # noqa: BLE001
        msg = f"Ошибка: {exc}"
    finally:
        await file.close()
    return RedirectResponse(url=f"{BP}/projects/{project_id}?msg={quote(msg)}", status_code=303)


def _goals_chart(stats: dict, names: dict, dr, gran: str, top: int = 8) -> dict | None:
    """Time series (at ``gran``) of favourite-goal completions across the
    project's URLs: a line per top goal + 'Прочие' for the rest."""
    from datetime import timedelta

    daily = stats.get("daily") or {}
    if not daily:
        return None
    span = (dr.end - dr.start).days

    def buckets(daymap):
        series = [{"date": (dr.start + timedelta(days=i)).isoformat(),
                   "count": daymap.get((dr.start + timedelta(days=i)).isoformat(), 0)}
                  for i in range(span + 1)]
        return totals_svc.bucket_series(series, gran)

    ranked = [g["id"] for g in stats["by_goal"]]
    labels, series = None, []
    for gid in ranked[:top]:
        b = buckets(daily.get(gid, {}))
        labels = [x["date"] for x in b]
        series.append({"name": names.get(gid, f"Цель {gid}"), "data": [x["count"] for x in b]})
    rest = ranked[top:]
    if rest:
        merged: dict[str, int] = {}
        for gid in rest:
            for d2, c in daily.get(gid, {}).items():
                merged[d2] = merged.get(d2, 0) + c
        b = buckets(merged)
        labels = labels or [x["date"] for x in b]
        series.append({"name": "Прочие", "data": [x["count"] for x in b]})
    return {"labels": labels or [], "series": series} if series else None


def _project_goals(db, project, site, dr, gran, brand_keys=None):
    """Metrica goal completions on the project's entrance pages (favourites),
    optionally restricted to the URLs of the selected brands."""
    from app.services import goals as goals_svc

    if site is None:
        return None
    vids, _ = _same_domain_site_ids(db, site)  # sites of this domain holding visits
    url_for_key = {}
    for (u,) in db.execute(
        select(ProjectUrl.url).where(ProjectUrl.project_id == project.id)
    ).all():
        k = goals_svc.page_key(u)
        if brand_keys is None or k in brand_keys:
            url_for_key.setdefault(k, u)
    if not url_for_key:
        return None
    favs = goals_svc.parse_favorites(project.favorite_goals)
    stats = goals_svc.goal_stats(db, vids, dr, url_for_key, favorites=(favs or None))
    names = goals_svc.goal_names(db, vids)
    return {"stats": stats, "names": names, "favorites": favs,
            "chart": _goals_chart(stats, names, dr, gran)}


@router.post("/ui/projects/{project_id}/goals")
def ui_project_goals(project_id: int, goal: list[int] = Form(default=[]),
                     db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    project.favorite_goals = ",".join(str(g) for g in goal) if goal else None
    db.commit()
    return RedirectResponse(url=f"{BP}/projects/{project_id}", status_code=303)


@router.get("/projects/{project_id}/compare")
def project_compare_page(request: Request, project_id: int, metric: str = "clicks",
                         a_start: str | None = None, a_end: str | None = None,
                         b_start: str | None = None, b_end: str | None = None,
                         clean: int = 0, ratio: float = 10.0, min_impr: int = 100,
                         device: str = "all", merge: int = 0, db: Session = Depends(get_db)):
    from app.services.multi_compare import ENGINE_LABELS, ENGINES, compare_project

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    period_a = parse_date_range(a_start, a_end)
    period_b = resolve_period_b(period_a, b_start, b_end)
    result = compare_project(db, project, metric, period_a, period_b,
                             exclude_bots=bool(clean), ratio=ratio, min_impr=min_impr,
                             device=device, merge=bool(merge))
    return templates.TemplateResponse(
        request,
        "project_compare.html",
        {
            "request": request,
            "project": project,
            "metric": result["metric"],
            "clean": bool(clean), "ratio": ratio, "min_impr": min_impr,
            "device": result["device"], "merge": bool(merge),
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
                 min_impr: int = 100, merge: int = 0, db: Session = Depends(get_db)):
    sites = _sites(db)
    site = _resolve_site(db, site_id)
    result = None
    merged = None
    period_a = parse_date_range(a_start, a_end)
    period_b = resolve_period_b(period_a, b_start, b_end)
    if site is not None and (a_start or b_start or site_id):
        ids = site.id
        if merge:
            mids = _same_domain_ids(db, site)
            if len(mids) > 1:
                ids = mids
                merged = {"count": len(mids), "domain": domain_of(site.property_uri)}
        page_ids = None
        if grouping in ("subset", "page", "query") and project_id:
            project = db.get(Project, project_id)
            if project is not None:
                page_ids = project_page_ids(db, ids, project)
        result = growth.compare(
            db, ids, metric, period_a=period_a, period_b=period_b,
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
            "merge": bool(merge), "merged": merged,
            "period_a": period_a,
            "period_b": period_b,
            "result": result,
        },
    )


def _indexing_ctx(db, request, site_id, a=None, b=None, msg=None, check=None, gran="day"):
    from app.services import indexing

    sites = _sites(db)
    site = _resolve_site(db, site_id)
    snapshots, history, cmp, can_capture = [], [], None, False
    if site is not None:
        can_capture = indexing.supports(site)
        snapshots = indexing.list_snapshots(db, site.id)
        # index size is a STOCK, not a flow — roll up by last snapshot per bucket
        history = totals_svc.bucket_series(indexing.count_history(db, site), gran, agg="last")
        dates = [s["date"] for s in snapshots]
        da = a or (dates[0] if dates else None)
        db_ = b or (dates[1] if len(dates) > 1 else None)
        if da and db_:
            cmp = indexing.compare_snapshots(db, site.id, date.fromisoformat(da), date.fromisoformat(db_))
            a, b = da, db_
    return {
        "request": request, "msg": msg, "sites": sites, "site": site,
        "snapshots": snapshots, "history": history, "cmp": cmp,
        "a": a, "b": b, "can_capture": can_capture, "check": check, "gran": gran,
    }


@router.get("/indexing")
def indexing_page(request: Request, site_id: int | None = None, a: str | None = None,
                  b: str | None = None, msg: str | None = None, gran: str = "day",
                  db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "indexing.html", _indexing_ctx(db, request, site_id, a, b, msg, gran=gran)
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


def _same_domain_site_ids(db: Session, site: Site) -> tuple[list[int], str]:
    """All site_ids of the same bare domain (any source) + that domain. Metrica
    rows live under whichever twin holds them, so 404/visit queries span them."""
    d = domain_of(site.property_uri)
    ids = [sid for sid, uri in db.execute(select(Site.id, Site.property_uri)).all()
           if d and domain_of(uri) == d] or [site.id]
    return ids, d


@router.get("/errors")
def errors_page(request: Request, site_id: str | None = None, start: str | None = None,
                end: str | None = None, markers: str | None = None, gran: str = "day",
                db: Session = Depends(get_db)):
    from app.services import not_found

    sites = _sites(db)
    dr = parse_date_range(start, end)
    sid = int(site_id) if site_id else None  # "" / None -> overview of all domains
    site = db.get(Site, sid) if sid else None
    ctx = {"request": request, "sites": sites, "site": site, "range": dr, "gran": gran,
           "stats": None, "overview": None, "domain": None,
           "markers": markers if markers is not None else ", ".join(not_found.DEFAULT_MARKERS)}
    if site is not None:  # detailed view for one domain
        ids, domain = _same_domain_site_ids(db, site)
        ctx["domain"] = domain
        ctx["stats"] = not_found.not_found_stats(db, ids, dr, markers, site_domain=domain, gran=gran)
    else:  # overview: a chart per domain that has any 404, last bucket vs previous
        ctx["overview"] = not_found.not_found_overview(db, dr, markers, gran=gran)
    return templates.TemplateResponse(request, "errors.html", ctx)


@router.get("/metrika")
def metrika_page(request: Request, site_id: int | None = None, start: str | None = None,
                 end: str | None = None, msg: str | None = None, db: Session = Depends(get_db)):
    from app.services import visits

    sites = _sites(db)
    site = _resolve_site(db, site_id)
    dr = parse_date_range(start, end)
    summary = None
    if site is not None:
        # Metrica data belongs to the domain, not to a GSC/Yandex property —
        # show the same-domain twin (any source) that actually holds the visits.
        vid = site.id
        d = domain_of(site.property_uri)
        mids = [sid for sid, uri in db.execute(select(Site.id, Site.property_uri)).all()
                if d and domain_of(uri) == d] or [site.id]
        if len(mids) > 1:
            from sqlalchemy import func

            from app.db.models import Visit
            counts = dict(db.execute(
                select(Visit.site_id, func.count()).where(Visit.site_id.in_(mids))
                .group_by(Visit.site_id)
            ).all())
            vid = max(mids, key=lambda i: (counts.get(i, 0), -i))
        summary = visits.summary(db, vid, dr)
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
                   merge: int = 0, db: Session = Depends(get_db)):
    from app.services.antifraud import analyze

    sites = _sites(db)
    site = _resolve_site(db, site_id)
    dr = parse_date_range(start, end)
    result, merged = None, None
    if site is not None:
        ids = site.id
        if merge:
            mids = _same_domain_ids(db, site)
            if len(mids) > 1:
                ids = mids
                merged = {"count": len(mids), "domain": domain_of(site.property_uri)}
        result = analyze(db, ids, dr, ratio_threshold=ratio, min_impressions=min_impr)
    return templates.TemplateResponse(
        request,
        "antifraud.html",
        {
            "request": request, "sites": sites, "site": site, "range": dr,
            "ratio": ratio, "min_impr": min_impr, "result": result,
            "merge": bool(merge), "merged": merged,
        },
    )
