"""Per-page Google index check (URL Inspection) and submission (Indexing API)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import ensure_sources
from app.db.base import SessionLocal
from app.db.models import Site
from app.providers import register_override
from app.providers.mock import MockProvider
from app.services import google_index as gi


class FakeGoogle(MockProvider):
    """MockProvider + the two Google index endpoints, deterministic & offline."""

    capabilities = MockProvider.capabilities | {"url_inspection", "index_submit"}

    def new_sc_service(self):
        return self  # offline fake — no real per-thread client needed

    def inspect_url(self, site_url, url, language="ru-RU", service=None):
        if "boom" in url:
            raise RuntimeError("network blew up")
        if "quota" in url:
            from googleapiclient.errors import HttpError

            class _R:
                status = 429
                reason = "Too Many Requests"

            raise HttpError(_R(), b'{"error":{"message":"quota exceeded"}}')
        indexed = "ghost" not in url
        return {
            "indexStatusResult": {
                "verdict": "PASS" if indexed else "NEUTRAL",
                "coverageState": "Submitted and indexed" if indexed else "Crawled - currently not indexed",
                "lastCrawlTime": "2026-06-01T10:00:00Z",
                "googleCanonical": url,
                "robotsTxtState": "ALLOWED",
                "pageFetchState": "SUCCESSFUL",
            }
        }

    def request_indexing(self, url, type_="URL_UPDATED"):
        if "noscope" in url:
            from google.auth.exceptions import RefreshError

            raise RefreshError("invalid_scope: Bad Request",
                               {"error": "invalid_scope", "error_description": "Bad Request"})
        if "fail" in url:
            raise RuntimeError("publish failed")
        return {"urlNotificationMetadata": {"url": url,
                "latestUpdate": {"url": url, "type": type_, "notifyTime": "2026-06-18T00:00:00Z"}}}


@pytest.fixture(autouse=True)
def _tmp_results(tmp_path, monkeypatch):
    monkeypatch.setattr(gi, "DATA_DIR", tmp_path)
    # deterministic + fast: one worker, no pacing sleeps (concurrency/rate are
    # exercised in production via env, not in these logic tests)
    monkeypatch.setattr(gi, "INSPECT_WORKERS", 1)
    monkeypatch.setattr(gi, "INSPECT_RATE", 10000.0)


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def _gsc_site() -> int:
    s = SessionLocal()
    gsc = ensure_sources(s)["gsc"]
    site = Site(source_id=gsc.id, property_uri="sc-domain:example.com", display_name="demo")
    s.add(site)
    s.commit()
    sid = site.id
    s.close()
    return sid


# ----- service: inspection -----
def test_run_inspect_counts_and_sorts():
    register_override("gsc", FakeGoogle())
    sid = _gsc_site()
    state = gi.run_inspect(sid, [
        "https://example.com/a",       # indexed
        "https://example.com/ghost",   # not indexed
        "/b",                          # bare path -> indexed
        "https://example.com/a",       # duplicate, deduped
        "   ",                         # blank, dropped
    ])
    assert state["total"] == 3
    assert state["in_count"] == 2 and state["out_count"] == 1
    assert state["running"] is False and state["finished"]
    assert state["rows"][0]["indexed"] is False  # not-indexed sorted first
    flags = {r["input"]: r["indexed"] for r in state["rows"]}
    assert flags["https://example.com/a"] is True
    assert flags["https://example.com/b"] is True
    assert flags["https://example.com/ghost"] is False
    # result persisted to the per-site file
    assert gi.load_result("inspect", sid)["in_count"] == 2


def test_run_inspect_per_url_error_does_not_kill_batch():
    register_override("gsc", FakeGoogle())
    sid = _gsc_site()
    state = gi.run_inspect(sid, ["https://example.com/a", "https://example.com/boom", "https://example.com/c"])
    assert state["total"] == 3 and state["done"] == 3
    errored = [r for r in state["rows"] if r["error"]]
    assert len(errored) == 1 and "blew up" in errored[0]["error"]


def test_run_inspect_quota_stops_batch():
    register_override("gsc", FakeGoogle())
    sid = _gsc_site()
    state = gi.run_inspect(sid, [
        "https://example.com/a", "https://example.com/quota", "https://example.com/c"])
    assert state["done"] == 2 and state["total"] == 3   # stopped after the 429
    assert state["error"] and "исчерпан" in state["error"]
    assert len(state["rows"]) == 2


# ----- service: submission -----
def test_run_submit_ok_and_failures():
    register_override("gsc", FakeGoogle())
    sid = _gsc_site()
    state = gi.run_submit(sid, [
        "https://example.com/x", "https://example.com/fail-y", "https://example.com/z"])
    assert state["total"] == 3 and state["ok"] == 2 and state["failed"] == 1
    assert state["running"] is False
    ok_rows = {r["url"]: r["ok"] for r in state["rows"]}
    assert ok_rows["https://example.com/x"] is True
    assert ok_rows["https://example.com/fail-y"] is False
    assert state["rows"][0]["notify_time"] or any(r["notify_time"] for r in state["rows"])


def test_run_submit_invalid_scope_stops_with_reauth_hint():
    # the exact failure the user hit: refresh token granted without the indexing scope
    register_override("gsc", FakeGoogle())
    sid = _gsc_site()
    state = gi.run_submit(sid, [
        "https://example.com/noscope-1", "https://example.com/b", "https://example.com/c"])
    assert state["done"] == 1 and state["total"] == 3   # stopped on the first call
    assert state["error"] and "Переподключите" in state["error"]
    assert state["ok"] == 0 and state["failed"] == 1


# ----- routes: guards -----
def test_check_route_rejects_non_gsc_provider(client):
    # default MockProvider lacks url_inspection -> feature unavailable
    register_override("gsc", MockProvider())
    sid = _gsc_site()
    r = client.post("/ui/indexing/google/check",
                    data={"site_id": sid, "urls_text": "https://example.com/a"})
    assert r.status_code == 200
    assert "только для сайтов Google Search Console" in r.text


def test_submit_route_requires_confirmation(client):
    register_override("gsc", FakeGoogle())
    sid = _gsc_site()
    r = client.post("/ui/indexing/google/submit",
                    data={"site_id": sid, "urls_text": "https://example.com/a"})  # no confirm
    assert r.status_code == 200
    assert "не подтверждена" in r.text


def test_check_route_starts_job(client, monkeypatch):
    register_override("gsc", FakeGoogle())
    sid = _gsc_site()
    calls = []
    monkeypatch.setattr(gi, "start_inspect", lambda site_id, urls: (calls.append((site_id, urls)) or (True, "ok-msg")))
    r = client.post("/ui/indexing/google/check",
                    data={"site_id": sid, "urls_text": "https://example.com/a\nhttps://example.com/b"})
    assert r.status_code == 200
    assert calls and calls[0][0] == sid
    assert calls[0][1] == ["https://example.com/a", "https://example.com/b"]


def test_submit_route_starts_job_with_confirm(client, monkeypatch):
    register_override("gsc", FakeGoogle())
    sid = _gsc_site()
    calls = []
    monkeypatch.setattr(gi, "start_submit",
                        lambda site_id, urls, type_="URL_UPDATED": (calls.append((site_id, urls, type_)) or (True, "ok")))
    r = client.post("/ui/indexing/google/submit",
                    data={"site_id": sid, "urls_text": "https://example.com/a", "confirm": "yes"})
    assert r.status_code == 200
    assert calls and calls[0][0] == sid and calls[0][2] == "URL_UPDATED"


def test_indexing_page_shows_google_section_for_gsc(client):
    register_override("gsc", FakeGoogle())
    sid = _gsc_site()
    r = client.get(f"/indexing?site_id={sid}")
    assert r.status_code == 200
    assert "Проверка индексации в Google" in r.text
    assert "Отправить страницы на индексацию в Google" in r.text


def test_check_export_not_indexed(client):
    register_override("gsc", FakeGoogle())
    sid = _gsc_site()
    gi.run_inspect(sid, ["https://example.com/a", "https://example.com/ghost"])
    r = client.post("/ui/indexing/google/check/export",
                    data={"site_id": sid, "export": "out:txt"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    lines = [ln for ln in r.text.splitlines() if ln]
    assert lines == ["https://example.com/ghost"]
