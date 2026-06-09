"""Project CRUD + URL-list upload."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Project, ProjectUrl, Site
from app.deps import get_db
from app.schemas import ProjectCreate
from app.utils import normalize_url

router = APIRouter(prefix="/api", tags=["projects"])


@router.post("/projects")
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    if db.get(Site, payload.site_id) is None:
        raise HTTPException(404, "site not found")
    project = Project(name=payload.name, site_id=payload.site_id)
    db.add(project)
    db.commit()
    return {"id": project.id, "name": project.name, "site_id": project.site_id}


@router.get("/projects")
def list_projects(db: Session = Depends(get_db)):
    rows = db.execute(select(Project).order_by(Project.id)).scalars().all()
    return [
        {"id": p.id, "name": p.name, "site_id": p.site_id, "url_count": len(p.urls)}
        for p in rows
    ]


@router.get("/projects/{project_id}")
def get_project(project_id: int, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    return {
        "id": project.id,
        "name": project.name,
        "site_id": project.site_id,
        "urls": [u.url for u in project.urls],
    }


def _parse_urls(text: str) -> list[str]:
    out = []
    for line in text.replace(",", "\n").splitlines():
        line = line.strip()
        if line:
            out.append(line)
    return out


@router.post("/projects/{project_id}/urls")
async def upload_urls(
    project_id: int,
    file: UploadFile | None = File(None),
    urls_text: str | None = Form(None),
    db: Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")

    raw = ""
    if file is not None:
        raw += (await file.read()).decode("utf-8", errors="ignore") + "\n"
    if urls_text:
        raw += urls_text

    existing = {u.normalized_url for u in project.urls}
    added = 0
    for url in _parse_urls(raw):
        norm = normalize_url(url)
        if norm and norm not in existing:
            db.add(ProjectUrl(project_id=project.id, url=url, normalized_url=norm))
            existing.add(norm)
            added += 1
    db.commit()
    return {"added": added, "total": len(existing)}
