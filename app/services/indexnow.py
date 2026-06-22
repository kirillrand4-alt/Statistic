"""IndexNow: push changed URLs to Bing & Yandex (and other IndexNow engines).

IndexNow needs no OAuth — ownership is proven by hosting a key file
``https://<host>/<key>.txt`` whose content is exactly the key. We generate/store
one key (AppSetting ``indexnow_key``, public — not a secret); the owner uploads
``<key>.txt`` to each site's root, then we POST the changed URL list in a single
request (≤10000 URLs) to the IndexNow endpoint, which shares it with all
participating engines (Bing, Yandex, Seznam, Naver). **Google does NOT support
IndexNow.**
"""
from __future__ import annotations

import logging
import secrets
import time

import httpx
from sqlalchemy.orm import Session

from app.db.models import AppSetting, Site
from app.utils import domain_of, normalize_url

logger = logging.getLogger(__name__)

# Submit to each engine's own endpoint (not just the neutral aggregator): Yandex
# validates the key itself, so a stale Microsoft/Bing key-cache 403 won't block it,
# and we can show a per-engine result.
ENDPOINTS = [
    ("Яндекс", "https://yandex.com/indexnow"),
    ("Bing", "https://www.bing.com/indexnow"),
]
KEY_SETTING = "indexnow_key"
MAX_URLS = 10000


def get_key(db: Session) -> str:
    """Return the shared IndexNow key, creating one on first use (32 hex chars)."""
    row = db.get(AppSetting, KEY_SETTING)
    if row and row.value:
        return row.value
    key = secrets.token_hex(16)
    if row is None:
        db.add(AppSetting(key=KEY_SETTING, value=key))
    else:
        row.value = key
    db.commit()
    return key


def key_file_url(host: str, key: str) -> str:
    return f"https://{host}/{key}.txt"


def info(db: Session, site: Site) -> dict:
    """Key + host + expected key-file URL for the indexing page UI."""
    key = get_key(db)
    host = domain_of(site.property_uri)
    return {"key": key, "host": host, "key_url": key_file_url(host, key)}


def _prep_urls(host: str, urls, cap: int = MAX_URLS) -> list[str]:
    """De-dupe, resolve bare paths against the host, keep only same-host URLs."""
    out, seen = [], set()
    for raw in urls:
        u = (raw or "").strip()
        if not u:
            continue
        cand = f"https://{host}{u}" if u.startswith("/") else u
        nrm = normalize_url(cand)
        if not nrm or nrm in seen:
            continue
        seen.add(nrm)
        if host and domain_of(cand) != host:  # IndexNow rejects cross-host lists
            continue
        out.append(cand)
        if len(out) >= cap:
            break
    return out


def _describe(status: int, text: str) -> str:
    return {
        200: "Принято — Bing и Яндекс получили список URL.",
        202: "Принято; ключ ещё проверяется. Убедись, что файл-ключ лежит на сайте.",
        400: "Неверный формат запроса.",
        403: "Ключ не подходит — проверь, что файл-ключ доступен в корне сайта.",
        422: "URL не с этого домена или ключ не совпадает с файлом на сайте.",
        429: "Слишком часто — попробуй позже.",
    }.get(status, f"Ответ IndexNow {status}: {(text or '')[:200]}")


def submit(db: Session, site: Site, raw_urls) -> dict:
    """Send the URL list to each IndexNow engine; returns per-engine results."""
    host = domain_of(site.property_uri)
    key = get_key(db)
    urls = _prep_urls(host, raw_urls)
    base = {"host": host, "key": key, "key_url": key_file_url(host, key), "count": len(urls)}
    if not urls:
        return {**base, "ok": False, "results": [],
                "message": f"Нет подходящих URL для домена {host}."}
    body = {"host": host, "key": key, "keyLocation": base["key_url"], "urlList": urls}
    headers = {"Content-Type": "application/json; charset=utf-8"}
    results = []
    for name, url in ENDPOINTS:
        status, text = 0, ""
        for attempt in range(2):  # one retry on network error / 429
            try:
                r = httpx.post(url, json=body, timeout=30, headers=headers)
                status, text = r.status_code, r.text
                if status != 429:
                    break
            except Exception as exc:  # noqa: BLE001
                text = str(exc)[:200]
            if attempt == 0:
                time.sleep(1)
        results.append({"engine": name, "status": status, "ok": status in (200, 202),
                        "message": _describe(status, text)})
    return {**base, "results": results, "ok": any(r["ok"] for r in results)}
