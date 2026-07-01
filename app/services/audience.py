"""Аудитория / охват — оценка размера уникальной аудитории методом повторного
отлова (capture-recapture: Линкольн–Петерсен/Чапмен для пар сайтов и Шнабель для
нескольких), по пересечению **IP + User-Agent** между сайтами за один день.

Идея: визиты из рекламы/поиска на разных сайтах владельца за один день — это
независимые «отловы» одной популяции. Доля пересечения (одни и те же IP+UA на
двух сайтах) позволяет оценить, сколько всего уникальных людей в этой популяции,
включая тех, кого в этот день не «поймали».

Оговорки (важно): IP≠человек (NAT/общие IP завышают пересечение → занижают
оценку; динамические/мобильные IP занижают пересечение → завышают оценку), а
сравнивающие покупатели заходят на несколько сайтов (нарушение независимости →
занижение). Поэтому это оценка порядка величины и динамики, не точное число.
Сырые IP наружу не отдаём.
"""
from __future__ import annotations

import math
from collections import defaultdict

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.db.models import Site, Visit
from app.providers.base import DateRange
from app.utils import domain_of


# --- именованные базы ключевых слов (для вкладок «Аудитория <бренд>») --------
def _kw_key(base: str) -> str:
    return f"audience_kw:{(base or '').strip().lower()}"


def get_kw_base(db: Session, base: str) -> str:
    from app.db.models import AppSetting
    row = db.get(AppSetting, _kw_key(base))
    return row.value if row and row.value else ""


def set_kw_base(db: Session, base: str, raw: str) -> None:
    from app.db.models import AppSetting
    key = _kw_key(base)
    row = db.get(AppSetting, key)
    val = (raw or "").strip()
    if row is None:
        db.add(AppSetting(key=key, value=val))
    else:
        row.value = val
    db.commit()


def sites_with_visits(db: Session) -> list[dict]:
    """Сайты, по которым есть визиты (для выбора в интерфейсе)."""
    from sqlalchemy import func
    rows = db.execute(
        select(Visit.site_id, func.count()).group_by(Visit.site_id)
    ).all()
    counts = dict(rows)
    out = []
    for sid, n in counts.items():
        s = db.get(Site, sid)
        if s is None:
            continue
        out.append({"id": sid, "domain": domain_of(s.property_uri) or s.property_uri,
                    "name": s.display_name or s.property_uri, "visits": n})
    out.sort(key=lambda x: x["visits"], reverse=True)
    return out


# --- фильтры источника/ключей ------------------------------------------------
import json as _json

_KW_EMPTY = {"", "(not set)", "0", "null", "none", "-", "undefined"}


def _has_kw(extra) -> bool:
    """У визита есть рекламный ключ (utm_term/lastUTMTerm с непустым значением)."""
    if not extra or "UTMTerm" not in extra:
        return False
    try:
        obj = _json.loads(extra)
    except Exception:  # noqa: BLE001
        return True  # UTMTerm присутствует в тексте — считаем, что ключ есть
    for k, v in obj.items():
        if k.lower().endswith("utmterm") and str(v).strip().lower() not in _KW_EMPTY:
            return True
    return False


def _source_ok(traffic, adv, search, extra, has_kw, source) -> bool:
    ad = traffic == "ad" or bool(adv) or (extra and "UTM" in extra)
    srch = traffic in ("organic", "search") or bool(search)
    if source == "ad_kw_search":   # реклама с ключами + весь поиск (реком., без прямых/ботов)
        return (ad and has_kw) or srch
    if source == "ad_kw":          # только реклама с ключами
        return ad and has_kw
    if source == "search":         # только поиск
        return srch
    if source == "ad_search":      # любая реклама + поиск
        return ad or srch
    return True                    # весь трафик (вкл. прямые — осторожно, боты)


def _kw_ok(start_url, referer, extra, needles) -> bool:
    if not needles:
        return True
    hay = f"{start_url or ''} {referer or ''} {extra or ''}".lower()
    return any(n in hay for n in needles)


# --- оценки capture-recapture ------------------------------------------------
def chapman(n1: int, n2: int, m: int):
    """Оценка Чапмена (2 выборки) + приближённый 95% интервал. None, если нет пересечения."""
    if m <= 0 or n1 <= 0 or n2 <= 0:
        return None
    n = (n1 + 1) * (n2 + 1) / (m + 1) - 1
    var = ((n1 + 1) * (n2 + 1) * (n1 - m) * (n2 - m)) / ((m + 1) ** 2 * (m + 2))
    half = 1.96 * math.sqrt(var) if var > 0 else 0.0
    return {"n": round(n), "lo": max(round(n - half), max(n1, n2)), "hi": round(n + half)}


def schnabel(sets: list[set]):
    """Оценка Шнабеля по нескольким выборкам (сайтам). None, если пересечений нет."""
    marked: set = set()
    num = den = 0
    for s in sets:
        num += len(s) * len(marked)
        den += len(s & marked)
        marked |= s
    if den <= 0:
        return None
    return max(round(num / den), len(marked))  # не меньше наблюдённого объединения


def estimate(db: Session, site_ids, dr: DateRange, source="ad_kw_search", keywords=None,
             mode="ip_ua", bucket="day") -> dict:
    """Собрать per-bucket (день/месяц) и period-level оценки уникальной аудитории.

    ``bucket`` = "day" (по умолч.) или "month" — гранулярность ряда ``days`` (для
    сравнения помесячно с Wordstat). Оговорка: за месяц популяция не «закрыта»
    (люди приходят/уходят), поэтому месячная оценка завышена относительно дневной —
    годится для тренда/сравнения, не как точное число.
    """
    site_ids = [int(s) for s in site_ids]
    needles = [k.strip().lower() for k in (keywords or []) if k and k.strip()]
    use_ua = mode != "ip"

    q = select(Visit.site_id, Visit.date, Visit.ip, Visit.device, Visit.os, Visit.browser,
               Visit.traffic_source, Visit.adv_engine, Visit.search_engine,
               Visit.start_url, Visit.referer, Visit.extra).where(
        Visit.site_id.in_(site_ids), Visit.date >= dr.start, Visit.date <= dr.end)

    per_bucket: dict = defaultdict(lambda: defaultdict(set))  # bucket(date) -> site_id -> {keys}
    per_site: dict = defaultdict(set)                          # site_id -> {keys over period}
    total = matched = with_ip = 0
    for (sid, d, ip, dev, os_, br, traffic, adv, search, su, ref, extra) in db.execute(q):
        total += 1
        if not _source_ok(traffic, adv, search, extra, _has_kw(extra), source):
            continue
        if not _kw_ok(su, ref, extra, needles):
            continue
        matched += 1
        ip = (ip or "").strip()
        if not ip:
            continue
        with_ip += 1
        key = f"{ip}#{dev or ''}|{os_ or ''}|{br or ''}" if use_ua else ip
        if d is not None:
            per_bucket[d.replace(day=1) if bucket == "month" else d][sid].add(key)
        per_site[sid].add(key)

    # --- по дням/месяцам: наблюдённое объединение + оценка Шнабеля по сайтам бакета ---
    days = []
    daily_ests = []
    all_keys: set = set()
    for d in sorted(per_bucket):
        sitesets = [per_bucket[d][sid] for sid in site_ids if per_bucket[d][sid]]
        union: set = set().union(*sitesets) if sitesets else set()
        all_keys |= union
        est = schnabel(sitesets) if len(sitesets) >= 2 else None
        if est:
            daily_ests.append(est)
        days.append({"date": d.isoformat(), "observed": len(union),
                     "estimate": est, "n_sites": len(sitesets)})

    # --- period-level: множества по сайтам за весь период ---
    sites = []
    for sid in site_ids:
        if per_site[sid]:
            s = db.get(Site, sid)
            sites.append({"id": sid, "name": (domain_of(s.property_uri) or s.property_uri)
                          if s else str(sid), "uniq": len(per_site[sid])})
    sites.sort(key=lambda x: x["uniq"], reverse=True)

    pairs = []
    chapmans = []
    id2name = {s["id"]: s["name"] for s in sites}
    present = [s["id"] for s in sites]
    for i, a in enumerate(present):
        for b in present[i + 1:]:
            na, nb = len(per_site[a]), len(per_site[b])
            m = len(per_site[a] & per_site[b])
            ch = chapman(na, nb, m)
            if ch:
                chapmans.append(ch["n"])
            pairs.append({"a": id2name[a], "b": id2name[b], "na": na, "nb": nb,
                          "overlap": m, "chapman": ch})
    pairs.sort(key=lambda p: p["overlap"], reverse=True)  # самые информативные пары сверху

    period_sets = [per_site[sid] for sid in present]
    period_estimate = schnabel(period_sets) if len(period_sets) >= 2 else None
    daily_ests.sort()
    daily_median = daily_ests[len(daily_ests) // 2] if daily_ests else None

    return {
        "total_visits": total, "matched_visits": matched, "with_ip": with_ip,
        "ip_coverage": (with_ip / matched) if matched else 0.0,
        "sites": sites, "pairs": pairs, "days": days,
        "period_observed": len(all_keys), "period_estimate": period_estimate,
        "period_band": ([min(chapmans), max(chapmans)] if chapmans else None),
        "daily_median": daily_median,
        "n_sites_present": len(present),
        "mode": "ip_ua" if use_ua else "ip", "source": source,
    }
