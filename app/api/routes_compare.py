"""Period-over-period growth/decline endpoint (feature 5)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.models import Project, Site
from app.deps import get_db, parse_date_range
from app.services import growth
from app.services.loaders import project_page_id_map

router = APIRouter(prefix="/api", tags=["compare"])


@router.get("/compare")
def compare(
    site_id: int,
    metric: str = "clicks",
    grouping: str = "site",
    project_id: int | None = None,
    a_start: str | None = None,
    a_end: str | None = None,
    b_start: str | None = None,
    b_end: str | None = None,
    min_impressions: int = 0,
    db: Session = Depends(get_db),
):
    if db.get(Site, site_id) is None:
        raise HTTPException(404, "site not found")

    period_a = parse_date_range(a_start, a_end)
    period_b = parse_date_range(b_start, b_end)

    page_ids = None
    if grouping in ("subset", "page", "query") and project_id:
        project = db.get(Project, project_id)
        if project is not None:
            page_ids = list(project_page_id_map(db, project.site_id, project).values())

    result = growth.compare(
        db,
        site_id,
        metric,
        period_a=period_a,
        period_b=period_b,
        grouping=grouping,
        page_ids=page_ids,
        min_impressions=min_impressions,
    )
    result["period_a"] = {"start": period_a.start.isoformat(), "end": period_a.end.isoformat()}
    result["period_b"] = {"start": period_b.start.isoformat(), "end": period_b.end.isoformat()}
    return result
