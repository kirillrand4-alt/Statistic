"""Miralinks catalog client.

Miralinks has no public API, so we replay the catalog's own AJAX request with the
owner's logged-in session cookie. The catalog is a DataTables (legacy) endpoint::

    POST https://www.miralinks.com/ajaxPort/loadDataTableDataCatalog
    Content-Type: application/x-www-form-urlencoded
    body: sEcho=..&iColumns=41&iDisplayStart=0&iDisplayLength=20&..&searchData={...}

The whole request (including the user's chosen filters in ``searchData``) is
captured once from the browser and stored as a *template*; here we only swap
``iDisplayStart`` / ``iDisplayLength`` to page through the entire filtered set.

The response is DataTables-legacy JSON::

    {"iTotalRecords":N, "iTotalDisplayRecords":M,
     "aaData":[{"0":"<html>",..,"DT_RowId":id, "rowData":{...clean fields...}}, ...]}

``rowData`` already carries every field cleanly (domain, ИКС, price, Ahrefs DR,
region, topics…), so no HTML parsing is needed.
"""
from __future__ import annotations

import json
import re
import time

import httpx

DEFAULT_URL = "https://www.miralinks.com/ajaxPort/loadDataTableDataCatalog"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36")


def set_param(body: str, name: str, value) -> str:
    """Replace ``name=...`` in a urlencoded body (append if missing)."""
    pat = re.compile(rf"(^|&)({re.escape(name)})=[^&]*")
    new, n = pat.subn(rf"\g<1>{name}={value}", body)
    return new if n else f"{body}&{name}={value}"


def _auth_failed(status_code: int, text: str) -> bool:
    """Cookie expired -> Miralinks serves an HTML login page or 401/403/redirect."""
    if status_code in (301, 302, 303, 307, 308, 401, 403):
        return True
    head = (text or "")[:600].lower()
    return "<html" in head or "<!doctype" in head or "login" in head and "json" not in head


class Miralinks:
    def __init__(self, cookie: str, body_template: str, url: str = DEFAULT_URL,
                 timeout: int = 60, retries: int = 4):
        self.cookie = (cookie or "").strip()
        self.body = (body_template or "").strip()
        self.url = url
        self.timeout = timeout
        self.retries = retries

    def _headers(self) -> dict:
        return {
            "Cookie": self.cookie,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "ru,en;q=0.9",
            "Origin": "https://www.miralinks.com",
            "Referer": "https://www.miralinks.com/catalog",
            "User-Agent": _UA,
        }

    def fetch_page(self, start: int, length: int) -> dict:
        """One catalog page. Returns the parsed JSON, or ``{"error": ...}``."""
        body = set_param(self.body, "iDisplayStart", start)
        body = set_param(body, "iDisplayLength", length)
        last: dict = {"error": "failed"}
        for attempt in range(self.retries):
            try:
                r = httpx.post(self.url, content=body.encode("utf-8"),
                               headers=self._headers(), timeout=self.timeout,
                               follow_redirects=False)
            except Exception as exc:  # noqa: BLE001 — network hiccup, retry
                last = {"error": f"{exc.__class__.__name__}: {exc}"}
                time.sleep(2 * (attempt + 1))
                continue
            if _auth_failed(r.status_code, r.text):
                return {"error": "auth", "status": r.status_code,
                        "detail": "Сессия Miralinks недействительна — обновите cookie в Настройках."}
            try:
                return r.json()
            except Exception:  # noqa: BLE001
                last = {"error": "non-json", "status": r.status_code, "raw": (r.text or "")[:200]}
                time.sleep(2 * (attempt + 1))
        return last


def _num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None


def _lang(rd: dict) -> str | None:
    raw = rd.get("langCode")
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(data, list) and data and isinstance(data[0], list) and data[0]:
            return str(data[0][0])
    except (ValueError, TypeError):
        pass
    return None


def map_row(rd: dict) -> dict:
    """Map a Miralinks ``rowData`` object to our DonorSite-shaped dict."""
    g = rd.get
    domain = g("topDomain") or g("Ground.folder_url_wl") or ""
    return {
        "external_id": str(g("Ground.id") or g("DT_RowId") or "").strip(),
        "domain": (domain or "").strip().lower(),
        "site_url": g("Ground.folder_url_wl"),
        "name": g("Ground.name"),
        "description": g("Ground.description"),
        "sqi": _num(g("Ground.sqi")),
        "cy": _num(g("Ground.cy")),
        "da": _num(g("Ground.da")),
        "ahrefs_dr": _num(g("Ground.ahrefs_dr")),
        "cf": _num(g("Ground.cf")),
        "tf": _num(g("Ground.tf")),
        "spamness": _num(g("Ground.spamness")),
        "indexed_percent": _num(g("Ground.indexed_percent")),
        "ya_indexed_count": _num(g("Ground.ya_indexed_count")),
        "google_indexed_count": _num(g("Ground.google_indexed_count")),
        "traffic": _num(g("Ground.traffic")),
        "traffic_interval": g("traffic.interval"),
        "ahrefs_traffic": _num(g("Ground.ahrefs_traffic")),
        "ahrefs_domains": _num(g("Ground.ahrefs_domains")),
        "ahrefs_keywords": _num(g("Ground.ahrefs_keywords")),
        "price_rur": _num(g("Ground.price_rur")),
        "price_usd": _num(g("Ground.price_usd")),
        "article_price_rur": _num(g("Ground.article_price_rur")),
        "region_id": _num(g("Ground.region_id")),
        "region": g("Region.title"),
        "topics": g("subj") or g("subjShort"),
        "lang": _lang(rd),
        "links_in_articles": _num(g("Ground.links_in_articles")),
        "articles_count": _num(g("Ground.articles_count")),
        "rating": _num(g("ground_user_assessment_avg")),
        "placement_time_min": _num(g("Ground.placement_time")),
        "last_placement": (g("Ground.last_placement") or None),
        "venality": _num(g("Ground.venality_int")),
        "is_exclusive": _num(g("Ground.is_exclusive")),
        "is_fast": 1 if g("isFast") else 0,
        "is_pr": _num(g("Ground.is_pr")),
        "trusted": _num(g("trusted")),
        "screenshot": g("screenShot"),
        "raw": json.dumps(rd, ensure_ascii=False),
    }


def parse_rows(payload: dict):
    """Yield mapped donor dicts from a catalog response's ``aaData``."""
    for item in (payload or {}).get("aaData") or []:
        rd = item.get("rowData") if isinstance(item, dict) else None
        if isinstance(rd, dict) and (rd.get("Ground.id") or rd.get("DT_RowId")):
            row = map_row(rd)
            if row["external_id"]:
                yield row


def total_records(payload: dict) -> int:
    """Filtered total the catalog reports (drives pagination)."""
    p = payload or {}
    for k in ("iTotalDisplayRecords", "totalFilteredRecords", "iTotalRecords"):
        v = _num(p.get(k))
        if v:
            return int(v)
    return 0
