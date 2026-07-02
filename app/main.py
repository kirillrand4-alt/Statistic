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
    import asyncio

    init_db()
    db = SessionLocal()
    try:
        bootstrap(db)
    finally:
        db.close()
    start_scheduler()
    if get_settings().enable_scheduler:
        try:  # catch up a missed daily collect (e.g. server was off at the cron hour)
            from app.scheduler.jobs import catch_up_if_overdue
            catch_up_if_overdue()
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("Startup catch-up failed")

    # Prime the page cache in the background so the first visit after a restart is
    # instant (the heavy dashboard aggregation is precomputed, not paid on request).
    warm_task = None
    settings = get_settings()
    if settings.page_cache_ttl > 0:
        async def _warm():
            from app import cache
            try:
                await asyncio.sleep(1.5)  # let startup settle before self-requesting
                n = await cache.warm(app, settings.base_path)
                logging.getLogger(__name__).info("Page cache warmed: %d page(s)", n)
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).warning("Page cache warmup failed", exc_info=False)
        warm_task = asyncio.create_task(_warm())
    try:
        yield
    finally:
        if warm_task is not None:
            warm_task.cancel()
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

    # Compress HTML/CSS/JS responses (added last = outermost, so it also compresses
    # pages served from the cache). Big win on slow/VPN links.
    from starlette.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=1024)

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
