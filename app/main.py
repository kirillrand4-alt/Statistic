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
from app.db.base import SessionLocal, init_db
from app.scheduler.jobs import shutdown_scheduler, start_scheduler
from app.web import STATIC_DIR

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
    app = FastAPI(title="SEO Статистика", version="0.1.0", lifespan=lifespan)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    for module in (
        routes_projects,
        routes_metrics,
        routes_compare,
        routes_export,
        routes_admin,
        routes_pages,
    ):
        app.include_router(module.router)

    @app.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok"}

    return app


app = create_app()
