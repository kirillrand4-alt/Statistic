"""Server-side HTML page cache: store semantics + middleware behaviour."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient

from app import cache


def setup_function():
    cache.flush()


def test_store_set_get_and_ttl_expiry():
    cache.set("k", b"hello", "text/html", ttl=60)
    assert cache.get("k") == (b"hello", "text/html")
    # ttl<=0 never stores
    cache.set("z", b"x", "text/html", ttl=0)
    assert cache.get("z") is None
    # an expired entry is dropped on read
    cache.set("k2", b"v", "text/html", ttl=-1)
    assert cache.get("k2") is None


def test_store_lru_eviction():
    for i in range(5):
        cache.set(f"k{i}", b"x", "text/html", ttl=60, max_entries=3)
    # only the 3 most-recent survive
    assert cache.get("k0") is None and cache.get("k1") is None
    assert cache.get("k2") is not None and cache.get("k4") is not None


def test_flush_clears_all():
    cache.set("a", b"1", "text/html", ttl=60)
    cache.set("b", b"2", "text/html", ttl=60)
    assert cache.flush() == 2
    assert cache.get("a") is None and cache.stats()["entries"] == 0


def _app():
    app = FastAPI()
    app.add_middleware(cache.PageCacheMiddleware, ttl=60, base_path="")
    state = {"n": 0}

    @app.get("/pages")
    def pages():
        state["n"] += 1
        return HTMLResponse(f"<p>{state['n']}</p>")

    @app.get("/admin")  # not in the cache allowlist
    def admin():
        state["n"] += 1
        return HTMLResponse(f"<p>admin {state['n']}</p>")

    @app.get("/api/data")  # JSON, never cached
    def data():
        state["n"] += 1
        return JSONResponse({"n": state["n"]})

    return TestClient(app)


def test_middleware_caches_and_busts():
    c = _app()
    r1 = c.get("/pages")
    assert r1.text == "<p>1</p>" and r1.headers["x-page-cache"] == "MISS"
    r2 = c.get("/pages")
    assert r2.text == "<p>1</p>" and r2.headers["x-page-cache"] == "HIT"  # served from cache
    r3 = c.get("/pages?nocache=1")
    assert r3.text == "<p>2</p>" and r3.headers["x-page-cache"] == "REFRESH"  # recomputed
    r4 = c.get("/pages")  # canonical entry was refreshed by nocache
    assert r4.text == "<p>2</p>" and r4.headers["x-page-cache"] == "HIT"


def test_middleware_varies_by_query_and_skips_msg():
    c = _app()
    a = c.get("/pages?domain=x")
    b = c.get("/pages?domain=y")
    assert a.text != b.text  # different query => different cache entry
    # flash-message pages are never cached
    m1 = c.get("/pages?msg=hi")
    m2 = c.get("/pages?msg=hi")
    assert "x-page-cache" not in {k.lower() for k in m1.headers} or m1.headers.get("x-page-cache") is None
    assert m1.text != m2.text  # recomputed each time


def test_middleware_skips_non_allowlisted_and_non_html():
    c = _app()
    a1 = c.get("/admin")
    a2 = c.get("/admin")
    assert a1.text != a2.text  # /admin not cached -> increments
    j1 = c.get("/api/data")
    j2 = c.get("/api/data")
    assert j1.json()["n"] != j2.json()["n"]  # JSON not cached
