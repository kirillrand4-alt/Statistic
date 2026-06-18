"""FastAPI application entry point.

Lifespan: create tables, seed sources + default site, start the APScheduler
collection job; stop the scheduler on shutdown.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import (
    routes_admin,
    routes_compare,
    routes_export,
    routes_metrics,
    routes_pages,
    routes_projects,
)
from app.bootstrap import bootstrap
from app.cache import PageCacheMiddleware
from app.config import get_settings
from app.formlimit import apply_upload_limit
from app.db.base import SessionLocal, init_db
from app.scheduler.jobs import shutdown_scheduler, start_scheduler
from app.web import STATIC_DIR, templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        bootstrap(db)
    finally:
        db.close()
    start_scheduler()
    try:
        yield
    finally:
        shutdown_scheduler()


def create_app() -> FastAPI:
    settings = get_settings()
    bp = settings.base_path  # "" or e.g. "/stat"
    apply_upload_limit(max(1, settings.max_upload_mb) * 1024 * 1024)

    app = FastAPI(
        title="SEO Статистика",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=f"{bp}/docs",
        openapi_url=f"{bp}/openapi.json",
    )

    # Make the configured prefix available to all templates for link/asset URLs.
    templates.env.globals["base_path"] = bp

    # Per-page "refresh now" link target: current URL + ?nocache=1.
    def _refresh_url(request) -> str:
        u = request.url.include_query_params(nocache=1)
        return u.path + ("?" + u.query if u.query else "")

    templates.env.globals["refresh_url"] = _refresh_url

    app.mount(f"{bp}/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Cache rendered analytics pages so they load instantly across browsers.
    if settings.page_cache_ttl > 0:
        app.add_middleware(PageCacheMiddleware, ttl=settings.page_cache_ttl, base_path=bp)

    for module in (
        routes_projects,
        routes_metrics,
        routes_compare,
        routes_export,
        routes_admin,
        routes_pages,
    ):
        app.include_router(module.router, prefix=bp)

    @app.get(f"{bp}/health", include_in_schema=False)
    def health():
        return {"status": "ok"}

    return app


app = create_app()
