"""Server-rendered HTML pages (Jinja2 + Chart.js)."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import CollectionRun, Project, ProjectUrl, Site, Source
from app.deps import get_db, parse_date_range
from app.providers.base import DateRange
from app.services import growth
from app.services import totals as totals_svc
from app.services.ctr import ctr_for_project
from app.services.loaders import project_page_id_map
from app.services.top_keyword import top_keywords_for_project
from app.utils import normalize_url
from app.web import templates

router = APIRouter(tags=["pages"], include_in_schema=False)


def _sites(db: Session) -> list[Site]:
    return db.execute(select(Site).order_by(Site.id)).scalars().all()


def _resolve_site(db: Session, site_id: int | None) -> Site | None:
    if site_id is not None:
        return db.get(Site, site_id)
    return db.execute(select(Site).order_by(Site.id).limit(1)).scalar_one_or_none()


@router.get("/")
def dashboard(request: Request, site_id: int | None = None, start: str | None = None,
              end: str | None = None, db: Session = Depends(get_db)):
    sites = _sites(db)
    site = _resolve_site(db, site_id)
    dr = parse_date_range(start, end)
    ctx = {
        "request": request,
        "sites": sites,
        "site": site,
        "projects": db.execute(select(Project).order_by(Project.id)).scalars().all(),
        "range": dr,
        "totals": None,
        "daily": [],
        "top_pages": [],
        "runs": db.execute(
            select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(10)
        ).scalars().all(),
        "gsc_site_url": get_settings().gsc_site_url,
    }
    if site is not None:
        ctx["totals"] = totals_svc.site_totals(db, site.id, dr)
        ctx["daily"] = totals_svc.site_daily(db, site.id, dr)
        ctx["top_pages"] = totals_svc.per_page_totals(db, site.id, dr)[:20]
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
    return RedirectResponse(url=f"/projects/{project.id}", status_code=303)


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
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


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
    return RedirectResponse(url=f"/?site_id={site_id}", status_code=303)


@router.get("/admin")
def admin_page(request: Request, db: Session = Depends(get_db)):
    s = get_settings()
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "request": request,
            "sites": _sites(db),
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
    return RedirectResponse(url=f"/?site_id={site.id}", status_code=303)


@router.post("/ui/sites/{site_id}/toggle")
def ui_toggle_site(site_id: int, db: Session = Depends(get_db)):
    site = db.get(Site, site_id)
    if site is not None:
        site.enabled = not site.enabled
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/ui/backfill")
def ui_backfill(site_id: int = Form(...), days: int = Form(90), db: Session = Depends(get_db)):
    from app.scheduler.jobs import run_backfill

    try:
        run_backfill(db, site_id, days)
    except Exception:  # noqa: BLE001 - surfaced via the runs table on /admin
        pass
    return RedirectResponse(url="/admin", status_code=303)


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


@router.get("/compare")
def compare_page(request: Request, site_id: int | None = None, metric: str = "clicks",
                 grouping: str = "site", project_id: int | None = None,
                 a_start: str | None = None, a_end: str | None = None,
                 b_start: str | None = None, b_end: str | None = None,
                 min_impressions: int = 0, db: Session = Depends(get_db)):
    sites = _sites(db)
    site = _resolve_site(db, site_id)
    result = None
    period_a = parse_date_range(a_start, a_end)
    if not b_start and not b_end:
        length = (period_a.end - period_a.start).days + 1
        b_end_d = period_a.start - timedelta(days=1)
        period_b = DateRange(start=b_end_d - timedelta(days=length - 1), end=b_end_d)
    else:
        period_b = parse_date_range(b_start, b_end)
    if site is not None and (a_start or b_start or site_id):
        page_ids = None
        if grouping in ("subset", "page", "query") and project_id:
            project = db.get(Project, project_id)
            if project is not None:
                page_ids = list(project_page_id_map(db, project.site_id, project).values())
        result = growth.compare(
            db, site.id, metric, period_a=period_a, period_b=period_b,
            grouping=grouping, page_ids=page_ids, min_impressions=min_impressions,
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
            "period_a": period_a,
            "period_b": period_b,
            "result": result,
        },
    )
