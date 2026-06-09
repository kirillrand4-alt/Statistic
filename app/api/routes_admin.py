"""Admin: trigger collection/backfill, status, create sites."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import CollectionRun, Site, Source
from app.deps import get_db
from app.schemas import SiteCreate

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/collect/run")
def trigger_collect(site_id: int | None = None, db: Session = Depends(get_db)):
    from app.scheduler.jobs import collect_site, compute_window, run_daily_collect

    if site_id is None:
        return {"results": run_daily_collect(db)}

    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, "site not found")
    dr = compute_window(db, site, date.today(), get_settings().collect_refetch_days)
    rows = collect_site(db, site, dr)
    return {"site_id": site_id, "rows": rows, "start": dr.start.isoformat(), "end": dr.end.isoformat()}


@router.post("/backfill")
def trigger_backfill(site_id: int, days: int = 480, db: Session = Depends(get_db)):
    from app.scheduler.jobs import run_backfill

    return {"rows": run_backfill(db, site_id, days)}


@router.get("/status")
def status(db: Session = Depends(get_db)):
    sites = db.execute(select(Site)).scalars().all()
    runs = (
        db.execute(select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(20))
        .scalars()
        .all()
    )
    return {
        "sites": [
            {"id": s.id, "property_uri": s.property_uri, "source": s.source.code, "enabled": s.enabled}
            for s in sites
        ],
        "runs": [
            {
                "id": r.id,
                "site_id": r.site_id,
                "job_type": r.job_type,
                "target_date": r.target_date.isoformat() if r.target_date else None,
                "status": r.status,
                "rows_written": r.rows_written,
                "error": r.error_text,
                "started_at": r.started_at.isoformat() if r.started_at else None,
            }
            for r in runs
        ],
    }


@router.post("/sites")
def create_site(payload: SiteCreate, db: Session = Depends(get_db)):
    source = db.execute(
        select(Source).where(Source.code == payload.source_code)
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(400, f"unknown source_code {payload.source_code!r}")
    site = Site(
        source_id=source.id,
        property_uri=payload.property_uri,
        display_name=payload.display_name or payload.property_uri,
        external_host_id=payload.external_host_id,
        enabled=True,
    )
    db.add(site)
    db.commit()
    return {"id": site.id, "property_uri": site.property_uri}
