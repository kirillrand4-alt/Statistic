"""Metric endpoints: sites, TOP-1 keywords, CTR, totals."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Project, Site
from app.deps import get_db, parse_date_range, resolve_period_b
from app.services import totals as totals_svc
from app.services.ctr import ctr_for_project
from app.services.multi_compare import build_compare_export, compare_project
from app.services.top_keyword import top_keywords_for_project

router = APIRouter(prefix="/api", tags=["metrics"])


def _range_dict(dr):
    return {"start": dr.start.isoformat(), "end": dr.end.isoformat()}


@router.get("/sites")
def list_sites(db: Session = Depends(get_db)):
    rows = db.execute(select(Site).order_by(Site.id)).scalars().all()
    return [
        {
            "id": s.id,
            "property_uri": s.property_uri,
            "display_name": s.display_name,
            "source": s.source.code,
            "enabled": s.enabled,
        }
        for s in rows
    ]


@router.get("/projects/{project_id}/top-keywords")
def top_keywords(
    project_id: int,
    start: str | None = None,
    end: str | None = None,
    order_by: str = "clicks",
    db: Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    dr = parse_date_range(start, end)
    return {
        "range": _range_dict(dr),
        "order_by": order_by,
        "items": top_keywords_for_project(db, project, dr, order_by),
    }


@router.get("/projects/{project_id}/ctr")
def project_ctr(
    project_id: int,
    start: str | None = None,
    end: str | None = None,
    db: Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    dr = parse_date_range(start, end)
    return {"range": _range_dict(dr), **ctr_for_project(db, project, dr)}


@router.get("/projects/{project_id}/compare")
def project_compare(
    project_id: int,
    metric: str = "clicks",
    a_start: str | None = None,
    a_end: str | None = None,
    b_start: str | None = None,
    b_end: str | None = None,
    clean: int = 0,
    ratio: float = 10.0,
    min_impr: int = 100,
    device: str = "all",
    merge: int = 0,
    format: str = "json",
    db: Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    period_a = parse_date_range(a_start, a_end)
    period_b = resolve_period_b(period_a, b_start, b_end)
    result = compare_project(db, project, metric, period_a, period_b,
                             exclude_bots=bool(clean), ratio=ratio, min_impr=min_impr,
                             device=device, merge=bool(merge))
    if format in ("csv", "xlsx"):
        filename, buf, media = build_compare_export(result, project, fmt=format)
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        return StreamingResponse(buf, media_type=media, headers=headers)
    return result


@router.get("/totals")
def totals(
    site_id: int,
    scope: str = "site",
    project_id: int | None = None,
    start: str | None = None,
    end: str | None = None,
    db: Session = Depends(get_db),
):
    if db.get(Site, site_id) is None:
        raise HTTPException(404, "site not found")
    dr = parse_date_range(start, end)

    if scope == "site":
        return {
            "range": _range_dict(dr),
            "scope": "site",
            "totals": totals_svc.site_totals(db, site_id, dr),
            "daily": totals_svc.site_daily(db, site_id, dr),
        }
    if scope == "project":
        project = db.get(Project, project_id) if project_id else None
        if project is None:
            raise HTTPException(400, "project_id required for scope=project")
        return {
            "range": _range_dict(dr),
            "scope": "project",
            "totals": totals_svc.subset_totals(db, project, dr),
        }
    if scope == "page":
        return {
            "range": _range_dict(dr),
            "scope": "page",
            "pages": totals_svc.per_page_totals(db, site_id, dr),
        }
    raise HTTPException(400, "scope must be site|project|page")
