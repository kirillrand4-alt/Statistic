"""Webvisor recordings web page + safe video serving."""
from __future__ import annotations

from fastapi.testclient import TestClient

import app.api.routes_pages as rp
from app.main import app


def test_webvisor_page_lists_and_serves(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "WEBVISOR_VIDEOS", tmp_path)
    (tmp_path / "3254872593794597010.webm").write_bytes(b"\x1aE\xdf\xa3fake-webm")
    with TestClient(app) as c:
        page = c.get("/webvisor")
        assert page.status_code == 200
        assert "Вебвизор" in page.text
        assert "3254872593794597010" in page.text

        assert c.get("/webvisor/video/3254872593794597010.webm").status_code == 200
        # wrong extension / path traversal are rejected
        assert c.get("/webvisor/video/evil.txt").status_code == 404
        assert c.get("/webvisor/video/..%2f..%2fsecret.webm").status_code == 404


def test_webvisor_page_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "WEBVISOR_VIDEOS", tmp_path)
    with TestClient(app) as c:
        page = c.get("/webvisor")
        assert page.status_code == 200
        assert "Записей пока нет" in page.text
