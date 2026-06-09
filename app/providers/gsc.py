"""Google Search Console provider (Phase 1).

Auth via a service account (recommended) or an OAuth refresh token, selected by
``GSC_AUTH_MODE``. Note: in service-account mode the service-account email must
be added as a user on the property in Search Console, otherwise requests 403.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import date

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings, get_settings
from app.providers.base import (
    DateRange,
    DeviceMetricRow,
    PageMetricRow,
    QueryMetricRow,
    SearchDataProvider,
    TotalsRow,
)
from app.utils import normalize_url

logger = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
ROW_LIMIT = 25000  # GSC max rows per request


def _is_retryable(exc: BaseException) -> bool:
    """Retry on HTTP 429 and 5xx from the Google API client."""
    try:
        from googleapiclient.errors import HttpError
    except Exception:  # pragma: no cover - import guard
        return False
    if isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", None)
        try:
            status = int(status)
        except (TypeError, ValueError):
            return False
        return status == 429 or 500 <= status < 600
    return False


class GSCProvider(SearchDataProvider):
    code = "gsc"
    capabilities = {
        "page_metrics", "query_metrics_per_url", "site_totals", "all_query_metrics", "device_metrics",
    }

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._svc = None

    def data_delay_days(self) -> int:
        return self.settings.gsc_data_delay_days

    # ----- auth & client -----
    def _credentials(self):
        from app.credentials import get_cred

        s = self.settings
        mode = get_cred("gsc_auth_mode", s.gsc_auth_mode)

        if mode == "service_account":
            from google.oauth2 import service_account

            sa_json = get_cred("gsc_sa_json")
            if sa_json:
                return service_account.Credentials.from_service_account_info(
                    json.loads(sa_json), scopes=[SCOPE]
                )
            return service_account.Credentials.from_service_account_file(
                s.gsc_service_account_file, scopes=[SCOPE]
            )

        if mode == "oauth":
            from google.oauth2.credentials import Credentials

            client_id = get_cred("gsc_oauth_client_id")
            client_secret = get_cred("gsc_oauth_client_secret")
            refresh_token = get_cred("gsc_oauth_refresh_token", s.gsc_oauth_refresh_token)
            if not (client_id and client_secret):  # fall back to a client-secrets file
                with open(s.gsc_oauth_client_file) as fh:
                    data = json.load(fh)
                data = data.get("installed") or data.get("web") or data
                client_id = client_id or data.get("client_id")
                client_secret = client_secret or data.get("client_secret")
            return Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret,
                scopes=[SCOPE],
            )
        raise ValueError(f"Unknown GSC auth mode: {mode!r}")

    def _service(self):
        if self._svc is None:
            from googleapiclient.discovery import build

            self._svc = build(
                "webmasters", "v3", credentials=self._credentials(), cache_discovery=False
            )
        return self._svc

    # ----- low-level query with retry + pagination -----
    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def _execute(self, site_url: str, body: dict) -> dict:
        return (
            self._service()
            .searchanalytics()
            .query(siteUrl=site_url, body=body)
            .execute()
        )

    def _paged_rows(self, site_url: str, body: dict) -> Iterable[dict]:
        start_row = 0
        while True:
            page_body = dict(body, rowLimit=ROW_LIMIT, startRow=start_row)
            resp = self._execute(site_url, page_body)
            rows = resp.get("rows", []) or []
            yield from rows
            if len(rows) < ROW_LIMIT:
                break
            start_row += ROW_LIMIT

    @staticmethod
    def _base_body(dr: DateRange, dimensions: list[str]) -> dict:
        return {
            "startDate": dr.start.isoformat(),
            "endDate": dr.end.isoformat(),
            "dimensions": dimensions,
            "type": "web",
            "dataState": "all",
        }

    # ----- provider interface -----
    def fetch_page_metrics(
        self, site, dr: DateRange, urls: list[str] | None = None
    ) -> Iterable[PageMetricRow]:
        body = self._base_body(dr, ["page", "date"])
        wanted = {normalize_url(u) for u in urls} if urls else None
        for r in self._paged_rows(site.property_uri, body):
            page_url, day = r["keys"][0], r["keys"][1]
            if wanted is not None and normalize_url(page_url) not in wanted:
                continue
            yield PageMetricRow(
                url=page_url,
                date=date.fromisoformat(day),
                clicks=int(r.get("clicks", 0)),
                impressions=int(r.get("impressions", 0)),
                ctr=float(r.get("ctr", 0.0)),
                position=float(r.get("position", 0.0)),
            )

    def fetch_query_metrics_for_url(
        self, site, url: str, dr: DateRange
    ) -> Iterable[QueryMetricRow]:
        body = self._base_body(dr, ["query", "date"])
        body["dimensionFilterGroups"] = [
            {"filters": [{"dimension": "page", "operator": "equals", "expression": url}]}
        ]
        for r in self._paged_rows(site.property_uri, body):
            query_text, day = r["keys"][0], r["keys"][1]
            yield QueryMetricRow(
                query=query_text,
                url=url,
                date=date.fromisoformat(day),
                clicks=int(r.get("clicks", 0)),
                impressions=int(r.get("impressions", 0)),
                ctr=float(r.get("ctr", 0.0)),
                position=float(r.get("position", 0.0)),
            )

    def fetch_site_totals(self, site, dr: DateRange) -> Iterable[TotalsRow]:
        body = self._base_body(dr, ["date"])
        for r in self._paged_rows(site.property_uri, body):
            yield TotalsRow(
                date=date.fromisoformat(r["keys"][0]),
                clicks=int(r.get("clicks", 0)),
                impressions=int(r.get("impressions", 0)),
                ctr=float(r.get("ctr", 0.0)),
                position=float(r.get("position", 0.0)),
            )

    def fetch_all_query_metrics(self, site, dr: DateRange) -> Iterable[QueryMetricRow]:
        """Per-page per-query daily metrics for the whole site (page+query dims)."""
        body = self._base_body(dr, ["page", "query", "date"])
        for r in self._paged_rows(site.property_uri, body):
            page_url, query_text, day = r["keys"][0], r["keys"][1], r["keys"][2]
            yield QueryMetricRow(
                query=query_text,
                url=page_url,
                date=date.fromisoformat(day),
                clicks=int(r.get("clicks", 0)),
                impressions=int(r.get("impressions", 0)),
                ctr=float(r.get("ctr", 0.0)),
                position=float(r.get("position", 0.0)),
            )

    def fetch_page_metrics_by_device(self, site, dr: DateRange) -> Iterable[DeviceMetricRow]:
        body = self._base_body(dr, ["page", "device", "date"])
        for r in self._paged_rows(site.property_uri, body):
            page_url, device, day = r["keys"][0], r["keys"][1], r["keys"][2]
            yield DeviceMetricRow(
                url=page_url,
                date=date.fromisoformat(day),
                device=str(device).lower(),
                clicks=int(r.get("clicks", 0)),
                impressions=int(r.get("impressions", 0)),
                ctr=float(r.get("ctr", 0.0)),
                position=float(r.get("position", 0.0)),
            )

    def list_sites(self) -> list[dict]:
        """List Search Console properties the credentials can access."""
        resp = self._service().sites().list().execute()
        out = []
        for entry in resp.get("siteEntry", []) or []:
            out.append(
                {"site_url": entry.get("siteUrl"), "permission": entry.get("permissionLevel")}
            )
        return out
