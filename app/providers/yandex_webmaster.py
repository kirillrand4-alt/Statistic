"""Yandex Webmaster provider (Phase 2).

Auth is a single OAuth token (`Authorization: OAuth <token>`). v1 delivers host
discovery + daily site totals (clicks/impressions/CTR/position) via the
search-queries history endpoint — which maps cleanly to the daily-snapshot
schema and powers the dashboard, period comparison and export for Yandex.
Per-URL query analytics (beta API) is added in a later iteration once verified
against a live token.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import Settings, get_settings
from app.providers.base import (
    DateRange,
    PageMetricRow,
    QueryMetricRow,
    SearchDataProvider,
    TotalsRow,
)
from app.utils import normalize_url

logger = logging.getLogger(__name__)
API = "https://api.webmaster.yandex.net/v4"


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or 500 <= code < 600
    return isinstance(exc, (httpx.TransportError,))


class YandexWebmasterProvider(SearchDataProvider):
    code = "yandex_webmaster"
    capabilities = {"site_totals", "page_metrics", "all_query_metrics"}

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._uid: int | None = None

    def _token(self) -> str:
        from app.credentials import get_cred

        token = get_cred("yandex_wm_token", self.settings.yandex_wm_oauth_token)
        if not token:
            raise ValueError("Не задан OAuth-токен Яндекс.Вебмастера.")
        return token

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def _get(self, path: str, params=None) -> dict:
        resp = httpx.get(
            f"{API}{path}",
            headers={"Authorization": f"OAuth {self._token()}"},
            params=params,
            timeout=30,
        )
        if resp.status_code != 200:
            raise ValueError(f"Yandex Webmaster {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def _post(self, path: str, body: dict) -> dict:
        resp = httpx.post(
            f"{API}{path}",
            headers={"Authorization": f"OAuth {self._token()}"},
            json=body,
            timeout=60,
        )
        if resp.status_code != 200:
            raise ValueError(f"Yandex Webmaster {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def user_id(self) -> int:
        if self._uid is None:
            self._uid = self._get("/user/")["user_id"]
        return self._uid

    def list_sites(self) -> list[dict]:
        data = self._get(f"/user/{self.user_id()}/hosts/")
        out = []
        for h in data.get("hosts", []) or []:
            url = h.get("ascii_host_url") or h.get("unicode_host_url") or h.get("host_id")
            out.append(
                {
                    "site_url": url,
                    "host_id": h.get("host_id"),
                    "permission": "verified" if h.get("verified") else "unverified",
                }
            )
        return out

    def fetch_site_totals(self, site, dr: DateRange) -> Iterable[TotalsRow]:
        host_id = site.external_host_id or site.property_uri
        params = [
            ("date_from", dr.start.isoformat()),
            ("date_to", dr.end.isoformat()),
            ("query_indicator", "TOTAL_SHOWS"),
            ("query_indicator", "TOTAL_CLICKS"),
            ("query_indicator", "AVG_SHOW_POSITION"),
        ]
        data = self._get(
            f"/user/{self.user_id()}/hosts/{host_id}/search-queries/all/history", params=params
        )
        ind = data.get("indicators", {}) or {}

        def series(name: str) -> dict[str, float]:
            return {p["date"][:10]: p.get("value") for p in ind.get(name, []) or []}

        shows, clicks, pos = series("TOTAL_SHOWS"), series("TOTAL_CLICKS"), series("AVG_SHOW_POSITION")
        for d in sorted(set(shows) | set(clicks)):
            impressions = int(shows.get(d) or 0)
            c = int(clicks.get(d) or 0)
            yield TotalsRow(
                date=date.fromisoformat(d),
                clicks=c,
                impressions=impressions,
                ctr=(c / impressions) if impressions else 0.0,
                position=float(pos.get(d) or 0.0),
            )

    # ----- query analytics (per-URL / per-query daily stats) -----
    def _full_url(self, site, path: str | None) -> str | None:
        if not path:
            return None
        if path.startswith("http"):
            return path
        base = (site.property_uri or "").rstrip("/")
        return base + path if path.startswith("/") else f"{base}/{path}"

    @staticmethod
    def _by_date(entry: dict) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for st in entry.get("statistics", []) or []:
            d = (st.get("date") or "")[:10]
            if d:
                out.setdefault(d, {})[st.get("field")] = st.get("value")
        return out

    def _query_analytics(self, host_id: str, text_indicator: str, dr: DateRange):
        offset, limit = 0, 500
        while True:
            body = {
                "offset": offset,
                "limit": limit,
                "device_type_indicator": "ALL",
                "text_indicator": text_indicator,
                "date_from": dr.start.isoformat(),
                "date_to": dr.end.isoformat(),
            }
            data = self._post(
                f"/user/{self.user_id()}/hosts/{host_id}/query-analytics/list", body
            )
            entries = data.get("text_indicator_to_statistics", []) or []
            yield from entries
            if len(entries) < limit:
                break
            offset += limit

    def fetch_page_metrics(self, site, dr: DateRange, urls=None) -> Iterable[PageMetricRow]:
        host_id = site.external_host_id or site.property_uri
        wanted = {normalize_url(u) for u in urls} if urls else None
        for e in self._query_analytics(host_id, "URL", dr):
            url = self._full_url(site, (e.get("text_indicator") or {}).get("value"))
            if not url or (wanted is not None and normalize_url(url) not in wanted):
                continue
            for d, f in self._by_date(e).items():
                impr, clk = int(f.get("IMPRESSIONS") or 0), int(f.get("CLICKS") or 0)
                yield PageMetricRow(
                    url=url, date=date.fromisoformat(d), clicks=clk, impressions=impr,
                    ctr=(clk / impr) if impr else 0.0, position=float(f.get("POSITION") or 0.0),
                )

    def fetch_all_query_metrics(self, site, dr: DateRange) -> Iterable[QueryMetricRow]:
        host_id = site.external_host_id or site.property_uri
        for e in self._query_analytics(host_id, "QUERY", dr):
            query = (e.get("text_indicator") or {}).get("value") or ""
            url = self._full_url(site, (e.get("popular_complementary_indicator") or {}).get("value"))
            for d, f in self._by_date(e).items():
                impr, clk = int(f.get("IMPRESSIONS") or 0), int(f.get("CLICKS") or 0)
                yield QueryMetricRow(
                    query=query, url=url, date=date.fromisoformat(d), clicks=clk, impressions=impr,
                    ctr=(clk / impr) if impr else 0.0, position=float(f.get("POSITION") or 0.0),
                )

    def fetch_query_metrics_for_url(self, site, url: str, dr: DateRange) -> Iterable[QueryMetricRow]:
        # collection uses fetch_all_query_metrics; this stays a no-op for now.
        return []
