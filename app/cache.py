"""In-process TTL cache for rendered HTML pages + the caching middleware.

The analytics pages recompute heavy aggregates (pandas over many daily rows) on
every request, so the first load from any browser is slow. We cache successful
GET HTML responses for a TTL (default 60 min), keyed by path+query and shared
across all browsers, so only the first hit after expiry pays the cost.

Force a refresh globally with :func:`flush`, or per-page by adding ``?nocache=1``
(the middleware recomputes and overwrites that page's entry).

Pages that must always be live (``/indexing`` job polling, ``/admin``, ``/ui/*``
actions, downloads, ``/webvisor`` video) are simply not in the allowlist.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Heavy analytics pages to prime on startup so the first real visitor gets a HIT.
WARM_PATHS = ("/", "/keywords", "/pages", "/errors", "/compare")

# key -> (expires_epoch, body, media_type)
_STORE: "OrderedDict[str, tuple[float, bytes, str]]" = OrderedDict()
_LOCK = threading.Lock()
_MAX_ENTRIES = 512

# Path prefixes (relative to the app base_path) that are safe to cache.
_CACHE_EXACT = {"/"}
_CACHE_PREFIXES = (
    "/projects/", "/keywords", "/pages", "/serp", "/cannibalization",
    "/donors", "/errors", "/compare", "/antifraud", "/metrika",
)


def get(key: str):
    """Return (body, media) if a fresh entry exists, else None."""
    now = time.time()
    with _LOCK:
        item = _STORE.get(key)
        if item is None:
            return None
        expires, body, media = item
        if expires < now:
            _STORE.pop(key, None)
            return None
        _STORE.move_to_end(key)  # LRU touch
        return body, media


def set(key: str, body: bytes, media: str, ttl: int, max_entries: int = _MAX_ENTRIES) -> None:
    if ttl <= 0:
        return
    with _LOCK:
        _STORE[key] = (time.time() + ttl, body, media)
        _STORE.move_to_end(key)
        while len(_STORE) > max_entries:
            _STORE.popitem(last=False)  # evict oldest


def flush() -> int:
    """Drop every cached page; returns how many were dropped."""
    with _LOCK:
        n = len(_STORE)
        _STORE.clear()
        return n


def stats() -> dict:
    now = time.time()
    with _LOCK:
        live = sum(1 for exp, _, _ in _STORE.values() if exp >= now)
        return {"entries": len(_STORE), "live": live}


async def warm(app, base_path: str = "", paths=WARM_PATHS) -> int:
    """Prime the page cache in-process by GETting the heavy pages through the ASGI
    app (no network port — works the same on Windows/Linux), so the first real
    visitor after a restart gets an instant HIT instead of paying the recompute.

    Best-effort: any page that errors is logged and skipped. Returns how many
    pages were primed successfully.
    """
    import httpx

    bp = base_path or ""
    primed = 0
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://warmup",
                                 timeout=180.0) as client:
        for rel in paths:
            path = f"{bp}/{rel.lstrip('/')}" if bp else rel
            try:
                r = await client.get(path)
                if r.status_code == 200:
                    primed += 1
                logger.info("cache warm %s -> %s (%s)", path, r.status_code,
                            r.headers.get("X-Page-Cache", "-"))
            except Exception:  # noqa: BLE001 — warmup is best-effort
                logger.warning("cache warm failed for %s", path, exc_info=False)
    return primed


def _rel_path(path: str, bp: str) -> str:
    if not bp:
        return path
    if path == bp:
        return "/"
    if path.startswith(bp + "/"):
        return path[len(bp):]
    return path


def _cacheable(rel: str) -> bool:
    return rel in _CACHE_EXACT or any(rel == p.rstrip("/") or rel.startswith(p) for p in _CACHE_PREFIXES)


def _norm_qs(query_params) -> str:
    """Stable query string excluding the cache-busting flag."""
    items = sorted((k, v) for k, v in query_params.multi_items() if k != "nocache")
    return "&".join(f"{k}={v}" for k, v in items)


class PageCacheMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, ttl: int = 3600, base_path: str = ""):
        super().__init__(app)
        self.ttl = ttl
        self.bp = base_path

    async def dispatch(self, request, call_next):
        if request.method != "GET":
            flush()  # a mutating request (form save etc.) — drop cached pages so the change shows at once
            return await call_next(request)
        if self.ttl <= 0:
            return await call_next(request)
        rel = _rel_path(request.url.path, self.bp)
        qp = request.query_params
        # skip flash-message pages (one-off, shown right after a redirect)
        if not _cacheable(rel) or "msg" in qp:
            return await call_next(request)
        force = "nocache" in qp
        key = rel + "?" + _norm_qs(qp)
        if not force:
            hit = get(key)
            if hit is not None:
                body, media = hit
                return Response(content=body, media_type=media, headers={"X-Page-Cache": "HIT"})
        response = await call_next(request)
        ctype = response.headers.get("content-type", "")
        if response.status_code == 200 and ctype.startswith("text/html"):
            body = b"".join([chunk async for chunk in response.body_iterator])
            set(key, body, ctype, self.ttl)
            return Response(content=body, status_code=200, media_type=ctype,
                            headers={"X-Page-Cache": "REFRESH" if force else "MISS"})
        return response
