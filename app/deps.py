"""Shared FastAPI dependencies and small helpers."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.db.base import get_db  # re-export for routers
from app.providers.base import DateRange

__all__ = ["get_db", "parse_date_range"]

DEFAULT_RANGE_DAYS = 28


def _parse(d: str | None) -> date | None:
    if not d:
        return None
    return datetime.strptime(d, "%Y-%m-%d").date()


def parse_date_range(start: str | None = None, end: str | None = None) -> DateRange:
    """Parse ISO ``start``/``end`` query params; default = last 28 days."""
    e = _parse(end) or (date.today() - timedelta(days=1))
    s = _parse(start) or (e - timedelta(days=DEFAULT_RANGE_DAYS - 1))
    if s > e:
        s = e
    return DateRange(start=s, end=e)


def resolve_period_b(period_a: DateRange, b_start: str | None, b_end: str | None) -> DateRange:
    """Period B from params, or the adjacent previous window of equal length."""
    if not b_start and not b_end:
        length = (period_a.end - period_a.start).days + 1
        b_end_d = period_a.start - timedelta(days=1)
        return DateRange(start=b_end_d - timedelta(days=length - 1), end=b_end_d)
    return parse_date_range(b_start, b_end)
