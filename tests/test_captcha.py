"""Pluggable captcha solver (Yandex SmartCaptcha via CapMonster/2Captcha)."""
from __future__ import annotations

import httpx
import pytest

from app.services import captcha as C


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    """Replays a queued list of JSON payloads for successive POSTs."""

    def __init__(self, queue):
        self._queue = queue
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None):
        self.calls.append((url, json))
        return _Resp(self._queue.pop(0))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(C.time, "sleep", lambda *_: None)


def _patch_client(monkeypatch, queue):
    holder = {}

    def factory(*a, **k):
        holder["client"] = _FakeClient(list(queue))
        return holder["client"]

    monkeypatch.setattr(httpx, "Client", factory)
    return holder


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("CAPTCHA_PROVIDER", "2Captcha")
    monkeypatch.setenv("CAPTCHA_API_KEY", "  abc123  ")
    prov, key = C.config()
    assert prov == "2captcha" and key == "abc123"


def test_config_default_provider(monkeypatch):
    monkeypatch.delenv("CAPTCHA_PROVIDER", raising=False)
    monkeypatch.delenv("CAPTCHA_API_KEY", raising=False)
    # no DB cred in this isolated test db -> default provider, no key
    prov, key = C.config()
    assert prov == "capmonster"


def test_solve_happy_path(monkeypatch):
    holder = _patch_client(monkeypatch, [
        {"errorId": 0, "taskId": 555},
        {"errorId": 0, "status": "processing"},
        {"errorId": 0, "status": "ready", "solution": {"token": "TOK-OK"}},
    ])
    token = C.solve_smartcaptcha("sk_1", "https://wordstat.yandex.ru/",
                                 provider="capmonster", api_key="k", timeout=30)
    assert token == "TOK-OK"
    calls = holder["client"].calls
    assert calls[0][0].endswith("/createTask")
    task = calls[0][1]["task"]
    assert task["type"] == "YandexSmartCaptchaTaskProxyless"
    assert task["websiteKey"] == "sk_1" and task["websiteURL"].startswith("https://")
    assert calls[1][0].endswith("/getTaskResult")


def test_solve_create_error(monkeypatch):
    _patch_client(monkeypatch, [
        {"errorId": 1, "errorCode": "ERROR_TASK_TYPE_NOT_SUPPORTED",
         "errorDescription": "not supported"},
    ])
    with pytest.raises(RuntimeError, match="ERROR_TASK_TYPE_NOT_SUPPORTED"):
        C.solve_smartcaptcha("sk", "https://x/", provider="capmonster", api_key="k")


def test_solve_unknown_provider():
    with pytest.raises(RuntimeError, match="неизвестный провайдер"):
        C.solve_smartcaptcha("sk", "https://x/", provider="nope", api_key="k")


def test_solve_no_key():
    with pytest.raises(RuntimeError, match="нет ключа"):
        C.solve_smartcaptcha("sk", "https://x/", provider="capmonster", api_key=None)
