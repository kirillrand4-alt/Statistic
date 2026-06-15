"""End-to-end API smoke test through the FastAPI app (MockProvider)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.providers import register_override
from app.providers.mock import DEFAULT_PAGES, MockProvider


@pytest.fixture()
def client():
    register_override("gsc", MockProvider())
    register_override("yandex_webmaster", MockProvider())
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_full_flow(client):
    # 1. create a site
    r = client.post(
        "/api/admin/sites",
        json={"source_code": "gsc", "property_uri": "sc-domain:example.com", "display_name": "demo"},
    )
    assert r.status_code == 200, r.text
    site_id = r.json()["id"]

    # 2. create a project + upload URLs
    r = client.post("/api/projects", json={"name": "P", "site_id": site_id})
    project_id = r.json()["id"]
    r = client.post(
        f"/api/projects/{project_id}/urls", data={"urls_text": "\n".join(DEFAULT_PAGES)}
    )
    assert r.json()["added"] == len(DEFAULT_PAGES)

    # 3. collect (mock) data
    r = client.post(f"/api/admin/collect/run?site_id={site_id}")
    assert r.status_code == 200, r.text
    assert r.json()["rows"] > 0

    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=20)
    qp = f"start={start}&end={end}"

    # 4. TOP-1 keywords
    r = client.get(f"/api/projects/{project_id}/top-keywords?{qp}")
    items = r.json()["items"]
    assert len(items) == len(DEFAULT_PAGES)
    assert any(i["top_query"] for i in items)

    # 5. site totals
    r = client.get(f"/api/totals?site_id={site_id}&scope=site&{qp}")
    assert r.json()["totals"]["clicks"] > 0

    # 6. compare (page grouping)
    b_end = start - timedelta(days=1)
    b_start = b_end - timedelta(days=20)
    r = client.get(
        f"/api/compare?site_id={site_id}&grouping=page&metric=clicks"
        f"&a_start={start}&a_end={end}&b_start={b_start}&b_end={b_end}"
    )
    assert "rows" in r.json()

    # 7. export CSV
    r = client.get(f"/api/export?site_id={site_id}&format=csv&level=page&{qp}")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]

    # 8. pages render
    assert client.get("/").status_code == 200
    assert client.get(f"/projects/{project_id}?{qp}").status_code == 200
    assert client.get(f"/projects/{project_id}?{qp}&gran=month").status_code == 200  # trend chart
    assert client.get(f"/compare?site_id={site_id}").status_code == 200
    assert client.get(f"/metrika?site_id={site_id}").status_code == 200
    assert client.get(f"/errors?{qp}").status_code == 200            # overview
    assert client.get(f"/errors?site_id={site_id}&{qp}").status_code == 200  # detail
    # time-granularity (day/week/month) renders on the trend pages
    assert client.get("/?gran=week").status_code == 200
    assert client.get(f"/?gran=month").status_code == 200
    assert client.get("/errors?gran=week").status_code == 200
    assert client.get("/keywords").status_code == 200
    assert client.get(f"/keywords?domain=example.com&{qp}").status_code == 200
    assert client.get(f"/keywords/export?domain=example.com&{qp}&format=csv").status_code == 200
    assert client.get(f"/indexing?site_id={site_id}&gran=month").status_code == 200

    # 9. two-engine project comparison (JSON, page, CSV export)
    r = client.get(f"/api/projects/{project_id}/compare?metric=clicks&a_start={start}&a_end={end}")
    assert r.status_code == 200
    body = r.json()
    assert "gsc" in body["engines"]
    assert len(body["rows"]) == len(DEFAULT_PAGES)
    assert client.get(f"/projects/{project_id}/compare").status_code == 200
    r = client.get(f"/api/projects/{project_id}/compare?format=csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]


def test_ui_admin_flows(client):
    # admin page renders even with no sites
    assert client.get("/admin").status_code == 200

    # create a site entirely through the UI form (TestClient follows the redirect)
    r = client.post(
        "/ui/sites",
        data={"source_code": "gsc", "property_uri": "sc-domain:example.com", "display_name": "demo"},
    )
    assert r.status_code == 200
    sites = client.get("/api/sites").json()
    assert len(sites) == 1
    site_id = sites[0]["id"]

    # project + URLs via UI
    assert client.post("/ui/projects", data={"name": "P", "site_id": site_id}).status_code == 200
    pid = client.get("/api/projects").json()[0]["id"]
    client.post(f"/ui/projects/{pid}/urls", data={"urls_text": "\n".join(DEFAULT_PAGES)})

    # backfill via UI (mock provider) then verify dashboard surfaces data
    assert client.post("/ui/backfill", data={"site_id": site_id, "days": 40}).status_code == 200
    page = client.get(f"/?site_id={site_id}")
    assert page.status_code == 200
    assert "Топ страниц" in page.text

    # enable/disable toggle
    assert client.post(f"/ui/sites/{site_id}/toggle").status_code == 200
    assert client.get("/api/sites").json()[0]["enabled"] is False


def test_gsc_connect_route_oauth(client):
    r = client.post(
        "/ui/gsc/connect",
        data={
            "mode": "oauth",
            "client_id": "cid",
            "client_secret": "csecret",
            "refresh_token": "rtoken",
            "backfill_days": "15",
        },
    )
    assert r.status_code == 200  # redirect to dashboard, followed
    # the site is registered synchronously (data pull runs in a background thread)
    assert len(client.get("/api/sites").json()) >= 1
    assert client.get("/admin").status_code == 200


def test_gsc_oauth_start_redirects_to_google(client):
    r = client.post(
        "/ui/gsc/oauth/start",
        data={"client_id": "cid", "client_secret": "sec", "backfill_days": "30"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "accounts.google.com" in loc
    assert "client_id=cid" in loc
    assert "webmasters.readonly" in loc
    assert "callback" in loc


def test_gsc_oauth_callback_bad_state(client):
    r = client.get("/oauth/callback?code=abc&state=wrong", follow_redirects=False)
    assert r.status_code == 303
    assert "/admin" in r.headers["location"]


def test_yandex_connect_route(client):
    r = client.post("/ui/yandex/connect", data={"token": "y0_xxx", "backfill_days": "15"})
    assert r.status_code == 200  # redirect to dashboard, followed
    sites = client.get("/api/sites").json()
    assert any(s["source"] == "yandex_webmaster" for s in sites)
