"""Period-over-period growth/decline endpoint (feature 5)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Project, Site
from app.deps import get_db, parse_date_range
from app.services import growth
from app.services.loaders import project_page_ids
from app.utils import domain_of

router = APIRouter(prefix="/api", tags=["compare"])


def _same_domain_ids(db: Session, site: Site) -> list[int]:
    d = domain_of(site.property_uri)
    rows = db.execute(
        select(Site.id, Site.property_uri).where(Site.source_id == site.source_id)
    ).all()
    return [sid for sid, uri in rows if d and domain_of(uri) == d] or [site.id]


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
    clean: int = 0,
    ratio: float = 10.0,
    min_impr: int = 100,
    merge: int = 0,
    db: Session = Depends(get_db),
):
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, "site not found")

    period_a = parse_date_range(a_start, a_end)
    period_b = parse_date_range(b_start, b_end)

    ids = _same_domain_ids(db, site) if merge else site_id
    page_ids = None
    if grouping in ("subset", "page", "query") and project_id:
        project = db.get(Project, project_id)
        if project is not None:
            page_ids = project_page_ids(db, ids, project)

    result = growth.compare(
        db,
        ids,
        metric,
        period_a=period_a,
        period_b=period_b,
        grouping=grouping,
        page_ids=page_ids,
        min_impressions=min_impressions,
        exclude_bots=bool(clean),
        ratio=ratio,
        min_impr=min_impr,
    )
    result["period_a"] = {"start": period_a.start.isoformat(), "end": period_a.end.isoformat()}
    result["period_b"] = {"start": period_b.start.isoformat(), "end": period_b.end.isoformat()}
    return result
