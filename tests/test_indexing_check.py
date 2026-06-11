"""Check an uploaded list of URLs against the latest indexed snapshot."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import ensure_sources
from app.db.base import SessionLocal
from app.db.models import Site
from app.providers import register_override
from app.providers.mock import MockProvider
from app.services import indexing


@pytest.fixture()
def client():
    register_override("gsc", MockProvider())
    register_override("yandex_webmaster", MockProvider())
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_check_urls_service(db, site):
    # no snapshot yet -> None
    assert indexing.check_urls(db, site, ["https://example.com/"]) is None

    indexing.capture_indexed_urls(db, site)  # MockProvider -> DEFAULT_PAGES
    res = indexing.check_urls(db, site, [
        "https://example.com/blog/seo-guide",   # indexed
        "/about",                               # indexed (bare path -> site domain)
        "https://example.com/ghost",            # not indexed
        "   ",                                  # blank, dropped
        "/about",                               # duplicate, deduped
    ])
    assert res["total"] == 3
    assert res["in_count"] == 2 and res["out_count"] == 1
    flags = {r["input"]: r["in_index"] for r in res["rows"]}
    assert flags["https://example.com/blog/seo-guide"] is True
    assert flags["/about"] is True
    assert flags["https://example.com/ghost"] is False
    assert res["rows"][0]["in_index"] is False  # not-indexed sorted first


def test_indexing_check_route(client):
    s = SessionLocal()
    gsc = ensure_sources(s)["gsc"]
    site = Site(source_id=gsc.id, property_uri="sc-domain:example.com", display_name="demo")
    s.add(site)
    s.commit()
    sid = site.id
    indexing.capture_indexed_urls(s, site)
    s.close()

    r = client.post("/ui/indexing/check", data={
        "site_id": sid,
        "urls_text": "https://example.com/about\nhttps://example.com/ghost-xyz",
    })
    assert r.status_code == 200
    assert "✓ да" in r.text and "✗ нет" in r.text


def _seed_site_with_snapshot() -> int:
    s = SessionLocal()
    gsc = ensure_sources(s)["gsc"]
    site = Site(source_id=gsc.id, property_uri="sc-domain:example.com", display_name="demo")
    s.add(site)
    s.commit()
    sid = site.id
    indexing.capture_indexed_urls(s, site)  # DEFAULT_PAGES incl. /about
    s.close()
    return sid


def test_indexing_export_not_indexed_txt(client):
    sid = _seed_site_with_snapshot()
    r = client.post("/ui/indexing/check/export", data={
        "site_id": sid,
        "urls_text": "https://example.com/about\nhttps://example.com/ghost-1\nhttps://example.com/ghost-2",
        "export": "out:txt",
    })
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "attachment" in r.headers["content-disposition"]
    assert ".txt" in r.headers["content-disposition"]
    lines = [ln for ln in r.text.splitlines() if ln]
    assert set(lines) == {"https://example.com/ghost-1", "https://example.com/ghost-2"}
    assert "https://example.com/about" not in lines  # indexed -> excluded


def test_indexing_export_all_csv(client):
    sid = _seed_site_with_snapshot()
    r = client.post("/ui/indexing/check/export", data={
        "site_id": sid,
        "urls_text": "https://example.com/about\nhttps://example.com/ghost",
        "export": "all:csv",
    })
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "url,in_index,title" in r.text
    assert "https://example.com/about,1" in r.text
    assert "https://example.com/ghost,0" in r.text


def test_indexing_check_route_no_snapshot(client):
    s = SessionLocal()
    gsc = ensure_sources(s)["gsc"]
    site = Site(source_id=gsc.id, property_uri="sc-domain:example.com", display_name="demo")
    s.add(site)
    s.commit()
    sid = site.id
    s.close()

    r = client.post("/ui/indexing/check", data={"site_id": sid, "urls_text": "https://example.com/"})
    assert r.status_code == 200
    assert "Сначала создайте снимок" in r.text
