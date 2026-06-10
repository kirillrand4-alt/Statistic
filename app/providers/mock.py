"""Deterministic mock provider for offline development and tests.

Generates reproducible metrics from a hash of (url|query, date) plus a mild
upward trend over time, so the dashboard, exports and growth comparisons all
show realistic, stable numbers without any API keys.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import date, timedelta

from app.providers.base import (
    DateRange,
    PageMetricRow,
    QueryMetricRow,
    SearchDataProvider,
    TotalsRow,
)

DEFAULT_PAGES = [
    "https://example.com/",
    "https://example.com/blog/seo-guide",
    "https://example.com/blog/keyword-research",
    "https://example.com/products/widget",
    "https://example.com/products/gadget",
    "https://example.com/about",
]

DEFAULT_QUERIES = {
    "https://example.com/": ["example", "example brand", "buy example"],
    "https://example.com/blog/seo-guide": ["seo guide", "what is seo", "seo tips"],
    "https://example.com/blog/keyword-research": [
        "keyword research",
        "find keywords",
        "keyword tool",
    ],
    "https://example.com/products/widget": ["widget", "best widget", "widget price"],
    "https://example.com/products/gadget": ["gadget", "buy gadget", "gadget review"],
    "https://example.com/about": ["example company", "about example"],
}


def _h(*parts: str) -> int:
    raw = "|".join(parts).encode("utf-8")
    return int(hashlib.sha1(raw).hexdigest(), 16)


def _daterange(dr: DateRange) -> Iterable[date]:
    d = dr.start
    while d <= dr.end:
        yield d
        d += timedelta(days=1)


class MockProvider(SearchDataProvider):
    code = "mock"
    capabilities = {
        "page_metrics", "query_metrics_per_url", "site_totals", "all_query_metrics", "indexed_urls",
    }

    def __init__(self, pages=None, queries_by_page=None):
        self.pages = pages or DEFAULT_PAGES
        self.queries_by_page = queries_by_page or DEFAULT_QUERIES

    def _metrics(self, key: str, day: date, base: int) -> tuple[int, int, float, float]:
        trend = 1.0 + (day.toordinal() % 90) / 300.0  # gentle drift over time
        impressions = int((base + _h(key, day.isoformat()) % base) * trend)
        impressions = max(impressions, 1)
        ctr = ((_h("ctr", key) % 25) + 1) / 100.0  # 1%..25%
        clicks = int(impressions * ctr)
        position = 1.0 + (_h("pos", key, day.isoformat()) % 1500) / 100.0  # 1.0..16.0
        ctr_actual = clicks / impressions if impressions else 0.0
        return clicks, impressions, ctr_actual, position

    def fetch_page_metrics(self, site, dr, urls=None):
        pages = urls or self.pages
        for url in pages:
            for day in _daterange(dr):
                c, i, ctr, pos = self._metrics(url, day, base=400)
                yield PageMetricRow(url=url, date=day, clicks=c, impressions=i, ctr=ctr, position=pos)

    def fetch_query_metrics_for_url(self, site, url, dr):
        for q in self.queries_by_page.get(url, ["generic query"]):
            for day in _daterange(dr):
                c, i, ctr, pos = self._metrics(f"{url}::{q}", day, base=120)
                yield QueryMetricRow(
                    query=q, url=url, date=day, clicks=c, impressions=i, ctr=ctr, position=pos
                )

    def fetch_all_query_metrics(self, site, dr):
        for url in self.pages:
            yield from self.fetch_query_metrics_for_url(site, url, dr)

    def list_sites(self):
        return [{"site_url": "sc-domain:example.com", "permission": "siteOwner"}]

    def fetch_indexed_urls(self, site, cap: int = 50000):
        for u in self.pages:
            yield {"url": u, "title": u, "last_access": None}

    def fetch_index_count_history(self, site):
        return [{"date": "2026-01-01", "count": len(self.pages)},
                {"date": "2026-02-01", "count": len(self.pages) + 2}]

    def fetch_site_totals(self, site, dr):
        for day in _daterange(dr):
            clicks = impressions = 0
            wpos = 0.0
            for url in self.pages:
                c, i, _, pos = self._metrics(url, day, base=400)
                clicks += c
                impressions += i
                wpos += pos * i
            # add untracked traffic so the site total exceeds tracked pages
            extra = _h("extra", day.isoformat()) % 500
            impressions += extra
            clicks += extra // 10
            ctr = clicks / impressions if impressions else 0.0
            position = wpos / impressions if impressions else 0.0
            yield TotalsRow(date=day, clicks=clicks, impressions=impressions, ctr=ctr, position=position)
