"""Заявки с рекламы — standalone report.

Ad-traffic visits (Metrica) that reached a chosen goal ("квал лид"), each with:
its **entrance page**, the **page where the goal completed** (exit URL — the best
proxy Metrica's logs give; there is no per-goal URL), and the **page path** the
user walked (visit.watch_ids -> Hit.url, when hits are downloaded).

Reads only the Visit/Hit tables and reuses goal helpers, so it doesn't touch any
other feature. Favourite goals are saved per scope (domain or "all") in AppSetting.
"""
from __future__ import annotations

import json
import re

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.db.models import AppSetting, Hit, Site, Visit
from app.providers.base import DateRange
from app.services.goals import goal_names, parse_goal_ids
from app.utils import domain_of

_INT = re.compile(r"\d+")

# "Спасибо"/подтверждение — заявку оставляют на странице ПЕРЕД ними, поэтому такие
# страницы пропускаем и показываем реальную страницу сайта. Подстроки в URL.
THANKYOU = (
    "spasibo", "thank", "blagodar", "success", "zayavka-prin", "order-success",
    "/sent", "/sended", "/spasibo", "/spsibo",
)


def _is_thankyou(url: str | None) -> bool:
    u = (url or "").lower()
    return any(t in u for t in THANKYOU)


def _is_real_page(url: str | None) -> bool:
    """A real site page — not a Metrica event pseudo-URL (goal://, form://, …)."""
    u = (url or "").lower()
    return u.startswith("http://") or u.startswith("https://")


def _lead_page(path: list[str], end_url: str | None) -> str | None:
    """Real site page the user was on before the thank-you/confirmation page:
    the last REAL page (http/https) in the path that is not a thank-you page.
    ``path`` is already filtered to real pages; falls back to the exit URL."""
    for u in reversed(path):
        if not _is_thankyou(u):
            return u
    return end_url


def _ad_clause():
    """Visit looks like advertising: source 'ad', a paid engine, or UTM-tagged."""
    return or_(
        Visit.traffic_source == "ad",
        and_(Visit.adv_engine.isnot(None), Visit.adv_engine != ""),
        Visit.extra.like("%UTM%"),
    )


def _sites_with_visits(db: Session):
    return select(Visit.site_id).distinct()


def visit_site_ids(db: Session, domain: str | None) -> list[int]:
    """site_ids holding visits — for one bare domain, or all (domain falsy)."""
    rows = db.execute(
        select(Site.id, Site.property_uri).where(Site.id.in_(_sites_with_visits(db)))
    ).all()
    if domain:
        return [sid for sid, uri in rows if domain_of(uri) == domain]
    return [sid for sid, _ in rows]


def domains_with_ads(db: Session, dr: DateRange) -> list[str]:
    """Bare domains that have ad-traffic visits in the period (for the picker)."""
    rows = db.execute(
        select(Site.property_uri).where(
            Site.id.in_(
                select(Visit.site_id).where(
                    Visit.date >= dr.start, Visit.date <= dr.end, _ad_clause()
                ).distinct()
            )
        )
    ).all()
    return sorted({d for (u,) in rows if (d := domain_of(u))})


def _utm(extra: str | None) -> dict:
    out: dict[str, str] = {}
    if not extra:
        return out
    try:
        for k, v in json.loads(extra).items():
            if "utm" in k.lower() and v:
                out[k.replace("lastUTM", "utm_").replace("UTM", "utm_").lower()] = v
    except Exception:  # noqa: BLE001
        pass
    return out


def available_goals(db: Session, site_ids: list[int], dr: DateRange) -> list[dict]:
    """Goals reached on ad visits in the period (id, name, visits) — for the picker."""
    if not site_ids:
        return []
    rows = db.execute(
        select(Visit.extra).where(
            Visit.site_id.in_(site_ids), Visit.date >= dr.start, Visit.date <= dr.end,
            Visit.extra.like("%goalsID%"), _ad_clause(),
        )
    ).all()
    counts: dict[int, int] = {}
    for (extra,) in rows:
        for g in parse_goal_ids(extra):
            counts[g] = counts.get(g, 0) + 1
    names = goal_names(db, site_ids) if counts else {}
    return sorted(
        ({"id": g, "name": names.get(g) or f"Цель {g}", "visits": c} for g, c in counts.items()),
        key=lambda r: r["visits"], reverse=True,
    )


def leads(db: Session, site_ids: list[int], dr: DateRange,
          goal_ids: set[int] | None = None, limit: int = 3000) -> list[dict]:
    """Ad visits that reached a (selected) goal — one row per visit."""
    if not site_ids:
        return []
    cols = (
        Visit.visit_id, Visit.site_id, Visit.date, Visit.date_time, Visit.start_url,
        Visit.end_url, Visit.watch_ids, Visit.extra, Visit.region_city, Visit.device,
        Visit.traffic_source, Visit.adv_engine,
    )
    rows = db.execute(
        select(*cols).where(
            Visit.site_id.in_(site_ids), Visit.date >= dr.start, Visit.date <= dr.end,
            Visit.extra.like("%goalsID%"), _ad_clause(),
        ).order_by(Visit.date_time.desc())
    ).all()
    want = set(goal_ids or [])
    picked, seen = [], set()
    need: dict[int, set] = {}
    for r in rows:
        vid = str(r[0])
        if vid in seen:
            continue
        sel = parse_goal_ids(r[7])
        sel = sel if not want else (sel & want)
        if not sel:
            continue
        seen.add(vid)
        picked.append((r, sorted(sel)))
        for w in _INT.findall(r[6] or ""):
            need.setdefault(r[1], set()).add(w)
        if len(picked) >= limit:
            break

    hit_url: dict[str, str] = {}
    for sid, wids in need.items():
        wl = list(wids)
        for i in range(0, len(wl), 800):
            for wid, url in db.execute(
                select(Hit.watch_id, Hit.url).where(
                    Hit.site_id == sid, Hit.watch_id.in_(wl[i:i + 800]))
            ).all():
                if url:
                    hit_url[str(wid)] = url

    names = goal_names(db, site_ids)
    out = []
    for r, sel in picked:
        path, s = [], set()
        for w in _INT.findall(r[6] or ""):
            u = hit_url.get(w)
            if u and _is_real_page(u) and u not in s:  # drop goal://, form:// events
                s.add(u)
                path.append(u)
        out.append({
            "visit_id": str(r[0]), "date": r[2].isoformat() if r[2] else None,
            "date_time": r[3], "entry": r[4], "goal_page": _lead_page(path, r[5]),
            "exit": r[5], "path": path,
            "utm": _utm(r[7]), "goals": [names.get(g) or f"Цель {g}" for g in sel],
            "city": r[8], "device": r[9], "source": r[11] or r[10],
        })
    return out


# ----- favourite goals per scope (domain or "all"), in AppSetting -----
def _fav_key(scope: str) -> str:
    return f"leads_goals:{scope or '__all__'}"


def get_favorites(db: Session, scope: str) -> set[int]:
    row = db.get(AppSetting, _fav_key(scope))
    return {int(m) for m in _INT.findall(row.value)} if row and row.value else set()


def set_favorites(db: Session, scope: str, goal_ids) -> None:
    val = ",".join(str(int(g)) for g in goal_ids)
    row = db.get(AppSetting, _fav_key(scope))
    if row is None:
        db.add(AppSetting(key=_fav_key(scope), value=val))
    else:
        row.value = val
    db.commit()
