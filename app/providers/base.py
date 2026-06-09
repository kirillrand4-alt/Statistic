"""Provider abstraction.

Every data source (GSC, Yandex Webmaster, Yandex Metrika) implements
:class:`SearchDataProvider` and returns the normalized dataclass rows below, so
the services and scheduler never touch vendor-specific JSON.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date


@dataclass
class PageMetricRow:
    url: str
    date: date
    clicks: int
    impressions: int
    ctr: float
    position: float


@dataclass
class QueryMetricRow:
    query: str
    date: date
    clicks: int
    impressions: int
    ctr: float
    position: float
    url: str | None = None


@dataclass
class TotalsRow:
    date: date
    clicks: int
    impressions: int
    ctr: float
    position: float


class SearchDataProvider(ABC):
    """Common interface for all search-data sources."""

    code: str = ""
    capabilities: set[str] = set()

    @abstractmethod
    def fetch_page_metrics(
        self, site, dr: DateRange, urls: list[str] | None = None
    ) -> Iterable[PageMetricRow]:
        """Per-page daily metrics, optionally restricted to ``urls``."""

    @abstractmethod
    def fetch_query_metrics_for_url(
        self, site, url: str, dr: DateRange
    ) -> Iterable[QueryMetricRow]:
        """Daily query metrics for a single page (drives TOP-1 keyword)."""

    @abstractmethod
    def fetch_site_totals(self, site, dr: DateRange) -> Iterable[TotalsRow]:
        """Whole-site daily totals."""

    # Optional — providers that can return all query rows at once override this
    # to save API calls during collection.
    def fetch_all_query_metrics(
        self, site, dr: DateRange
    ) -> Iterable[QueryMetricRow]:
        raise NotImplementedError

    def data_delay_days(self) -> int:
        """How many recent days are typically not yet finalized."""
        return 0
