"""Pluggable solver for Yandex SmartCaptcha (used by the Wordstat scraper).

CapMonster Cloud, Anti-Captcha and 2Captcha all expose the SAME JSON API
(``createTask`` / ``getTaskResult``), so one client works for all three — only
the base URL differs. For Yandex SmartCaptcha the task type is
``YandexSmartCaptchaTaskProxyless`` and the page's ``websiteKey`` (sitekey) +
``websiteURL`` are required; the solver returns a ``token`` to inject back into
the page.

⚠ Yandex SmartCaptcha support is confirmed for **2Captcha**; CapMonster's
support is not guaranteed (it may answer ``ERROR_TASK_TYPE_NOT_SUPPORTED``). The
provider is configurable, so switch to ``2captcha`` if CapMonster refuses — the
calling code falls back to a manual solve in any case.

Config (env wins, then the encrypted credential store):
  CAPTCHA_PROVIDER  capmonster | anticaptcha | 2captcha   (default capmonster)
  CAPTCHA_API_KEY   the solver account key                (or cred captcha_api_key)
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

BASES = {
    "capmonster": "https://api.capmonster.cloud",
    "anticaptcha": "https://api.anti-captcha.com",
    "2captcha": "https://api.2captcha.com",
}
TASK_TYPE = "YandexSmartCaptchaTaskProxyless"


def config() -> tuple[str, str | None]:
    """Return ``(provider, api_key)`` from env, falling back to stored creds."""
    provider = (os.getenv("CAPTCHA_PROVIDER") or "").strip().lower()
    key = (os.getenv("CAPTCHA_API_KEY") or "").strip()
    if not provider or not key:
        try:
            from app.credentials import get_cred
            provider = provider or (get_cred("captcha_provider") or "").strip().lower()
            key = key or (get_cred("captcha_api_key") or "").strip()
        except Exception:  # noqa: BLE001 — creds optional / DB may be absent
            pass
    return (provider or "capmonster"), (key or None)


def save_config(provider: str | None = None, api_key: str | None = None) -> None:
    """Persist provider/key in the encrypted credential store (set_cred)."""
    from app.credentials import set_cred
    if provider:
        set_cred("captcha_provider", provider.strip().lower())
    if api_key:
        set_cred("captcha_api_key", api_key.strip())


def solve_smartcaptcha(sitekey: str, page_url: str, *, provider: str | None = None,
                       api_key: str | None = None, timeout: float = 150.0) -> str:
    """Solve a Yandex SmartCaptcha and return its token.

    Raises ``RuntimeError`` on any failure (no key, unsupported task type,
    timeout) — callers should catch and fall back to a manual solve.
    """
    import httpx

    if not provider or not api_key:
        cprov, ckey = config()
        provider = provider or cprov
        api_key = api_key or ckey
    if not api_key:
        raise RuntimeError("нет ключа решателя капч (CAPTCHA_API_KEY / cred captcha_api_key)")
    base = BASES.get(provider)
    if not base:
        raise RuntimeError(f"неизвестный провайдер капч {provider!r}; "
                           f"доступны: {', '.join(BASES)}")

    with httpx.Client(timeout=30.0) as cli:
        r = cli.post(f"{base}/createTask", json={
            "clientKey": api_key,
            "task": {"type": TASK_TYPE, "websiteURL": page_url, "websiteKey": sitekey},
        })
        r.raise_for_status()
        data = r.json()
        if data.get("errorId"):
            raise RuntimeError(f"{provider}: {data.get('errorCode')} "
                               f"{data.get('errorDescription', '')}".strip())
        task_id = data["taskId"]

        deadline = time.monotonic() + timeout
        delay = 5.0
        while time.monotonic() < deadline:
            time.sleep(delay)
            r = cli.post(f"{base}/getTaskResult",
                         json={"clientKey": api_key, "taskId": task_id})
            r.raise_for_status()
            res = r.json()
            if res.get("errorId"):
                raise RuntimeError(f"{provider}: {res.get('errorCode')} "
                                   f"{res.get('errorDescription', '')}".strip())
            if res.get("status") == "ready":
                token = (res.get("solution") or {}).get("token")
                if not token:
                    raise RuntimeError(f"{provider}: пустой токен в решении")
                return token
            delay = 3.0  # poll faster after the first wait
    raise RuntimeError(f"{provider}: тайм-аут решения капчи ({timeout:.0f}с)")
