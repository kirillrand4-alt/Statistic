"""404 / "страница не найдена" analytics, built from Yandex Metrica hits.

A custom 404 page that carries the Metrica counter still records a pageview —
its *title* is the 404 page's title (e.g. "Страница не найдена"). We surface
those pageviews: how many, on which (non-existent) URLs, where the user came
from (referrer) and — the part the dashboard splits out — through which traffic
channel: ad / search / direct / ... . The data is the already-synced ``Hit``
table, so this page is live with no extra collection step.

Detection is by title markers (configurable), so it depends on the 404 page
both carrying the counter and having a recognisable title.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

from sqlalchemy import distinct, func, or_, select

from app.db.models import Hit, Site
from app.providers.base import DateRange
from app.services.totals import bucket_label, bucket_series
from app.utils import domain_of

# Substrings a 404 page <title> usually contains. Matched case-insensitively
# (see _title_filter); editable from the page.
DEFAULT_MARKERS = [
    "404", "не найден", "не найдена", "не существует",
    "ничего не найдено", "not found", "страница удалена",
]

# Metrica ym:pv:lastTrafficSource -> human channel. Anything else -> "Прочее".
_CHANNELS = {
    "ad": "Реклама", "organic": "Поиск", "direct": "Прямые",
    "internal": "Внутренние", "referral": "Ссылки", "social": "Соцсети",
    "recommend": "Рекомендации", "email": "Email", "messenger": "Мессенджеры",
}
# The three buckets the per-URL table breaks out (the rest fold into "other").
_BUCKET = {"Реклама": "ad", "Поиск": "search", "Прямые": "direct"}


def _channel(traffic_source: str | None) -> str:
    return _CHANNELS.get((traffic_source or "").lower(), "Прочее")


def parse_markers(raw: str | None) -> list[str]:
    """Comma-separated markers from the form, or the defaults."""
    if not raw:
        return list(DEFAULT_MARKERS)
    out = [m.strip() for m in raw.split(",") if m.strip()]
    return out or list(DEFAULT_MARKERS)


def _title_filter(markers: list[str]):
    """Case-insensitive title match that works on SQLite too (its LIKE is only
    ASCII-case-insensitive, so we OR a few Cyrillic-aware case variants)."""
    needles: set[str] = set()
    for m in markers:
        needles.update({m, m.lower(), m.upper(), m.capitalize()})
    return or_(*[Hit.title.like(f"%{n}%") for n in needles])


def _ids(site_ids) -> list[int]:
    if isinstance(site_ids, Iterable) and not isinstance(site_ids, (str, bytes)):
        return list(site_ids)
    return [site_ids]


def not_found_overview(db, dr: DateRange, markers_raw: str | None = None,
                       gran: str = "day") -> list[dict]:
    """One row per DOMAIN that has any 404 in ``dr``: series at ``gran`` (day /
    week / month, for a chart), plus the count on the LAST bucket with 404 data
    and the % change vs the previous one (follows the data frontier — useful when
    the sync lags and the calendar "yesterday" isn't downloaded yet).

    Domains are bare hosts, so a domain's same-host properties are summed. Sorted
    by the last-bucket count (then period total), so the busiest 404s float up.
    """
    markers = parse_markers(markers_raw)
    where = [Hit.date >= dr.start, Hit.date <= dr.end, Hit.is_page_view == 1,
             Hit.title.isnot(None), _title_filter(markers)]
    rows = db.execute(
        select(Hit.site_id, Hit.date, func.count()).where(*where)
        .group_by(Hit.site_id, Hit.date)
    ).all()
    if not rows:
        return []

    dom_of, site_for_dom = {}, {}  # site_id -> domain; domain -> representative site
    for sid, uri in db.execute(select(Site.id, Site.property_uri)).all():
        d = domain_of(uri)
        dom_of[sid] = d
        if d and (d not in site_for_dom or sid < site_for_dom[d]):
            site_for_dom[d] = sid

    per: dict[str, dict[date, int]] = {}
    for sid, d, c in rows:
        dom = dom_of.get(sid)
        if dom and d is not None:
            per.setdefault(dom, {})[d] = per.setdefault(dom, {}).get(d, 0) + int(c)

    out = []
    span = (dr.end - dr.start).days
    for dom, daymap in per.items():
        daily = [{"date": (dr.start + timedelta(days=i)).isoformat(),
                  "count": daymap.get(dr.start + timedelta(days=i), 0)}
                 for i in range(span + 1)]
        daily = bucket_series(daily, gran)  # chart series at the chosen granularity
        # last vs previous BUCKET that actually has 404s
        bmap: dict[str, int] = {}
        for d, c in daymap.items():
            lbl = bucket_label(d, gran)
            bmap[lbl] = bmap.get(lbl, 0) + c
        keys = sorted(bmap)
        last_lbl = keys[-1]
        prev_lbl = keys[-2] if len(keys) > 1 else None
        last = bmap[last_lbl]
        prev = bmap[prev_lbl] if prev_lbl else 0
        out.append({
            "domain": dom, "site_id": site_for_dom.get(dom), "daily": daily,
            "total": sum(daymap.values()),
            "last": last, "prev": prev,
            "last_date": last_lbl, "prev_date": prev_lbl,
            "delta_pct": ((last - prev) / prev * 100.0) if prev else None,
        })
    out.sort(key=lambda r: (r["last"], r["total"]), reverse=True)
    return out


def not_found_stats(db, site_ids, dr: DateRange, markers_raw: str | None = None,
                    site_domain: str | None = None, top: int = 30,
                    gran: str = "day") -> dict:
    """404 pageviews over ``dr`` for the given site(s): totals, daily trend,
    channel split, top URLs (with ad/search/direct breakdown) and top referrers
    (internal vs external)."""
    markers = parse_markers(markers_raw)
    where = [Hit.site_id.in_(_ids(site_ids)), Hit.date >= dr.start, Hit.date <= dr.end,
             Hit.is_page_view == 1, Hit.title.isnot(None), _title_filter(markers)]

    total, uniq = db.execute(
        select(func.count(), func.count(distinct(Hit.url))).where(*where)
    ).one()
    out: dict = {"markers": markers, "total": int(total or 0), "unique_urls": int(uniq or 0)}
    if not total:
        return out

    out["daily"] = bucket_series([
        {"date": d.isoformat(), "count": int(c)}
        for d, c in db.execute(
            select(Hit.date, func.count()).where(*where).group_by(Hit.date).order_by(Hit.date)
        ).all() if d is not None
    ], gran)

    # --- by traffic channel (ad / search / direct / ...) ---------------------
    channels: dict[str, int] = {}
    for ts, c in db.execute(
        select(Hit.traffic_source, func.count()).where(*where).group_by(Hit.traffic_source)
    ).all():
        ch = _channel(ts)
        channels[ch] = channels.get(ch, 0) + int(c)
    out["by_channel"] = sorted(
        ({"key": k, "count": v} for k, v in channels.items()),
        key=lambda r: r["count"], reverse=True,
    )
    out["ad"] = channels.get("Реклама", 0)
    out["search"] = channels.get("Поиск", 0)
    out["direct"] = channels.get("Прямые", 0)

    # --- top 404 URLs, each split into ad / search / direct / other ----------
    top_rows = db.execute(
        select(Hit.url, func.count()).where(*where)
        .group_by(Hit.url).order_by(func.count().desc()).limit(top)
    ).all()
    urls = [u for u, _ in top_rows]
    split: dict[str, dict] = {}
    if urls:
        for u, ts, c in db.execute(
            select(Hit.url, Hit.traffic_source, func.count())
            .where(*where, Hit.url.in_(urls)).group_by(Hit.url, Hit.traffic_source)
        ).all():
            d = split.setdefault(u, {"ad": 0, "search": 0, "direct": 0, "other": 0})
            d[_BUCKET.get(_channel(ts), "other")] += int(c)
    out["top_urls"] = [
        dict(url=u, count=int(c),
             **split.get(u, {"ad": 0, "search": 0, "direct": 0, "other": 0}))
        for u, c in top_rows
    ]

    # --- top referrers (which links lead to the 404), internal vs external ---
    ref_rows = db.execute(
        select(Hit.referer, func.count()).where(*where)
        .group_by(Hit.referer).order_by(func.count().desc()).limit(top)
    ).all()
    top_referers = []
    for ref, c in ref_rows:
        if not ref:
            top_referers.append({"referer": "(прямой / без реферера)", "count": int(c),
                                 "kind": "none"})
        else:
            internal = bool(site_domain) and domain_of(ref) == site_domain
            top_referers.append({"referer": ref, "count": int(c),
                                 "kind": "internal" if internal else "external"})
    out["top_referers"] = top_referers

    # internal/external/direct totals across ALL referrers (not just the top)
    no_ref = db.execute(
        select(func.count()).where(*where, or_(Hit.referer.is_(None), Hit.referer == ""))
    ).scalar() or 0
    internal = 0
    if site_domain:
        internal = db.execute(
            select(func.count()).where(*where, Hit.referer.like(f"%{site_domain}%"))
        ).scalar() or 0
    out["from_internal"] = int(internal)
    out["from_external"] = max(int(total) - int(internal) - int(no_ref), 0)
    out["from_direct"] = int(no_ref)
    return out
