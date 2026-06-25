"""Yandex Metrica goal completions attributed to the ENTRANCE (landing) page.

A visit carries the goals it reached (ym:s:goalsID, kept in the JSON ``extra``)
and its entrance page (ym:s:startURL -> ``start_url``). So per project URL we
can show how many visits that LANDED there completed a chosen ("favourite")
goal. Pages are matched by host+path (scheme / www / query-string-agnostic), so
landing URLs with UTM tags still match the clean project URL.

Goal data comes from the already-synced ``Visit`` table; goal names are fetched
from the Metrica API best-effort (falls back to the id).
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from sqlalchemy import func, select

from app.db.models import Visit
from app.providers.base import DateRange
from app.utils import as_id_list as _ids

_INT = re.compile(r"\d+")
_API = "https://api-metrika.yandex.net"
_NAME_CACHE: dict[int, dict[int, str]] = {}  # counter_id -> {goal_id: name}


def parse_goal_ids(extra_json: str | None) -> set[int]:
    """Goal ids reached in a visit, from its ``extra`` JSON. ym:s:goalsID is a
    list rendered like '[123,456]' / '123,456' / '123'."""
    if not extra_json or "goalsID" not in extra_json:
        return set()
    try:
        raw = json.loads(extra_json).get("goalsID")
    except (ValueError, TypeError):
        return set()
    if raw in (None, "", "[]"):
        return set()
    return {int(m) for m in _INT.findall(str(raw))}


def parse_favorites(raw: str | None) -> set[int]:
    return {int(m) for m in _INT.findall(raw)} if raw else set()


def page_key(url: str | None) -> str:
    """host+path key for matching (scheme / www / query / trailing slash ignored)."""
    p = urlsplit(url if "://" in (url or "") else "http://" + (url or ""))
    host = (p.netloc or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host + (p.path.rstrip("/") or "/")


def goal_stats(db, site_ids, dr: DateRange, url_for_key: dict[str, str],
               favorites: set[int] | None = None, top: int = 100) -> dict:
    """One pass over the domain's goal-bearing visits in ``dr`` that LANDED on a
    project URL:
      - ``available``: [{id, visits}] goals reached (for the picker),
      - ``by_goal``:   [{id, count}] completions per favourite goal,
      - ``by_url``:    [{url, count}] completions per entrance URL,
      - ``total``:     all favourite-goal completions.
    A completion = (visit reached goal); a visit reaching N favourite goals adds
    N. ``favorites=None`` counts every goal (the default when none are saved)."""
    rows = db.execute(
        select(Visit.visit_id, Visit.start_url, Visit.date, Visit.extra).where(
            Visit.site_id.in_(_ids(site_ids)), Visit.date >= dr.start, Visit.date <= dr.end,
            Visit.extra.isnot(None), Visit.extra.like("%goalsID%"),
        )
    ).all()
    available: dict[int, int] = {}
    by_goal: dict[int, int] = {}
    by_url: dict[str, int] = {}
    daily: dict[int, dict[str, int]] = {}  # goal_id -> {date_iso: completions}
    total = 0
    seen: set = set()  # one visit can sit under several same-domain properties
    for vid, start_url, d, extra in rows:
        if vid in seen:  # count each Metrica visit once, not per property
            continue
        seen.add(vid)
        k = page_key(start_url)
        if k not in url_for_key:
            continue
        goals = parse_goal_ids(extra)
        if not goals:
            continue
        for g in goals:
            available[g] = available.get(g, 0) + 1
        sel = goals if not favorites else (goals & favorites)
        if not sel:
            continue
        by_url[k] = by_url.get(k, 0) + len(sel)
        total += len(sel)
        diso = d.isoformat() if d is not None else None
        for g in sel:
            by_goal[g] = by_goal.get(g, 0) + 1
            if diso:
                daily.setdefault(g, {})[diso] = daily.setdefault(g, {}).get(diso, 0) + 1
    return {
        "available": sorted(({"id": g, "visits": v} for g, v in available.items()),
                            key=lambda r: r["visits"], reverse=True),
        "by_goal": sorted(({"id": g, "count": c} for g, c in by_goal.items()),
                          key=lambda r: r["count"], reverse=True),
        "by_url": sorted(({"url": url_for_key[k], "count": c} for k, c in by_url.items()),
                         key=lambda r: r["count"], reverse=True)[:top],
        "daily": daily,
        "total": total,
        "pages_with_goals": len(by_url),
    }


def _goal_names_for_counter(counter: int) -> dict[int, str]:
    """Goal id -> name for ONE Metrica counter (Management API, cached). Includes
    DELETED goals (useDeleted=true) — they still appear in historical visits, so
    without this they'd show as bare 'Цель <id>'."""
    if counter in _NAME_CACHE:
        return _NAME_CACHE[counter]

    import httpx

    from app.config import get_settings
    from app.credentials import get_cred

    token = (get_cred("yandex_metrika_token") or get_cred("yandex_wm_token")
             or get_settings().yandex_metrika_oauth_token)
    names: dict[int, str] = {}
    if token:
        try:
            r = httpx.get(f"{_API}/management/v1/counter/{counter}/goals",
                          params={"useDeleted": "true"},
                          headers={"Authorization": f"OAuth {token}"}, timeout=10)
            if r.status_code == 200:
                for g in r.json().get("goals", []):
                    if g.get("id") is not None:
                        names[int(g["id"])] = g.get("name") or f"Цель {g['id']}"
        except Exception:  # noqa: BLE001 — names are optional, fall back to ids
            pass
    if names:  # cache only a successful fetch (so transient failures retry later)
        _NAME_CACHE[counter] = names
    return names


def goal_names(db, site_ids) -> dict[int, str]:
    """Goal id -> name across ALL Metrica counters present in the visits, so goals
    from a second counter (or deleted ones) aren't left as bare ids."""
    counters = db.execute(
        select(Visit.counter_id).where(
            Visit.site_id.in_(_ids(site_ids)), Visit.counter_id.isnot(None)
        ).group_by(Visit.counter_id)
    ).scalars().all()
    names: dict[int, str] = {}
    for c in counters:
        try:
            names.update(_goal_names_for_counter(int(c)))
        except (TypeError, ValueError):
            continue
    return names
