"""Pick Yandex Metrica visits to record from Webvisor — from already-synced visits.

Webvisor session replays have **no API**: they are only viewable in the Metrica
web UI (a DOM/event replay, not a video file). So the actual recording is done by
browser automation (a separate recorder). This module answers only *which*
sessions to record and *how many*, for a period, from our ``visit`` table — so
the job can be sized before it runs.

Note: Metrica keeps Webvisor recordings for a limited time (≈15 days), so visits
older than that can't be replayed regardless of what's in our base.
"""
from __future__ import annotations

from sqlalchemy import func, select

from app.db.models import Site, Visit
from app.providers.base import DateRange
from app.utils import domain_of


def resolve_visit_site(db, site_id: int | None = None, domain: str | None = None) -> int | None:
    """The site_id that actually holds the visits for a domain (Metrica data is
    attached to whichever same-domain twin was loaded; pick the one with the most
    visits). Pass an explicit ``site_id`` to skip resolution."""
    if site_id and not domain:
        return site_id
    rows = db.execute(select(Site.id, Site.property_uri)).all()
    if domain:
        ids = [sid for sid, uri in rows if domain_of(uri) == domain]
    elif site_id:
        d = domain_of(next((uri for sid, uri in rows if sid == site_id), "") or "")
        ids = [sid for sid, uri in rows if d and domain_of(uri) == d]
    else:
        ids = [sid for sid, _ in rows]
    if not ids:
        return site_id
    counts = dict(db.execute(
        select(Visit.site_id, func.count()).where(Visit.site_id.in_(ids)).group_by(Visit.site_id)
    ).all())
    return max(ids, key=lambda i: (counts.get(i, 0), -i))


def sites_with_visits(db) -> list[dict]:
    """Domains/sites that actually have synced visits (for CLI hints), busiest first."""
    counts = dict(db.execute(
        select(Visit.site_id, func.count()).group_by(Visit.site_id)
    ).all())
    out = [{"site_id": sid, "domain": domain_of(uri), "visits": int(counts[sid])}
           for sid, uri in db.execute(select(Site.id, Site.property_uri)).all()
           if counts.get(sid)]
    out.sort(key=lambda r: r["visits"], reverse=True)
    return out


def has_robot_flag(db, site_id) -> bool:
    """Whether this site's visits carry the robot flag yet (i.e. were synced after
    ym:s:isRobot was added). If not, ``exclude_bots`` can't filter anything."""
    return db.execute(
        select(func.count()).select_from(Visit)
        .where(Visit.site_id == site_id, Visit.is_robot.isnot(None))
    ).scalar_one() > 0


def _where(site_id, dr: DateRange, source, min_page_views, min_duration, exclude_bots):
    w = [Visit.site_id == site_id, Visit.date >= dr.start, Visit.date <= dr.end]
    if source:
        w.append(Visit.traffic_source == source)
    if min_page_views:
        w.append(Visit.page_views >= min_page_views)
    if min_duration:
        w.append(Visit.duration > min_duration)            # «длиннее N секунд» (strictly >)
    if exclude_bots:
        # ym:s:isRobot != 1; NULL = not (re)synced yet → kept (filter is a no-op
        # until visits are re-synced with the robot field).
        w.append(func.coalesce(Visit.is_robot, 0) == 0)
    return w


def count_sessions(db, site_id, dr: DateRange, *, source=None, min_page_views=0,
                   min_duration=0, exclude_bots=False) -> int:
    return int(db.execute(
        select(func.count()).select_from(Visit)
        .where(*_where(site_id, dr, source, min_page_views, min_duration, exclude_bots))
    ).scalar_one())


def sessions_for_period(db, site_id, dr: DateRange, *, source=None, min_page_views=0,
                        min_duration=0, exclude_bots=False, limit: int | None = None) -> list[dict]:
    """Visits to record, newest first: visit_id (+ context for naming/filtering)."""
    stmt = (
        select(Visit.visit_id, Visit.counter_id, Visit.date, Visit.traffic_source,
               Visit.page_views, Visit.duration, Visit.start_url)
        .where(*_where(site_id, dr, source, min_page_views, min_duration, exclude_bots))
        .order_by(Visit.date.desc(), Visit.visit_id.desc())
    )
    if limit:
        stmt = stmt.limit(limit)
    out = []
    for vid, cid, d, src, pv, dur, surl in db.execute(stmt).all():
        out.append({"visit_id": vid, "counter_id": cid,
                    "date": d.isoformat() if d else None, "source": src,
                    "page_views": int(pv or 0), "duration": int(dur or 0), "start_url": surl})
    return out
