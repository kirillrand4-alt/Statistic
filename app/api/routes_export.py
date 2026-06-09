"""Export endpoint (feature 4): CSV / XLSX download for a date range."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db.models import Project, Site
from app.deps import get_db, parse_date_range
from app.services.export import build_export

router = APIRouter(prefix="/api", tags=["export"])


@router.get("/export")
def export(
    site_id: int,
    start: str | None = None,
    end: str | None = None,
    level: str = "page",
    format: str = "xlsx",
    project_id: int | None = None,
    db: Session = Depends(get_db),
):
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, "site not found")
    dr = parse_date_range(start, end)
    project = db.get(Project, project_id) if project_id else None
    fmt = "csv" if format == "csv" else "xlsx"

    filename, buf, media = build_export(db, site, dr, level=level, project=project, fmt=fmt)
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(buf, media_type=media, headers=headers)
