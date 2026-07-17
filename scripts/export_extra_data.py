"""Выгрузка «сверх поиска» по всем сайтам отдельными CSV в папку — Метрика,
индекс, 404, SERP. Дополняет scripts/export_search_data.py (там клики/показы/позиция).

Уровни (--levels):
  visits   — визиты Метрики по URL входа: визиты, ср. время, отказы, разбивка по
             источнику (Поиск/Реклама/Прямые/Прочее)
  goals    — достижения целей Метрики по URL входа С РАСШИФРОВКОЙ названий целей
  index    — страницы в индексе Яндекса (последний снимок), URL + заголовок
  notfound — 404 по хитам Метрики (битый URL, просмотры, разбивка по источнику)
  serp     — ТОП-10 Арсенкина (последний срез): ключ, ПС, регион, позиция, URL, домен

    python scripts/export_extra_data.py --out C:\seostat\drop\drop-storage --days 30
    python scripts/export_extra_data.py --out ... --levels goals,visits --per-host
    python scripts/export_extra_data.py --out ... --end-of-week --days 28
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
from sqlalchemy import func, or_, select  # noqa: E402

from app.db.base import SessionLocal, init_db  # noqa: E402
from app.db.models import Hit, IndexedUrlSnapshot, SerpResult, Site, Visit  # noqa: E402
from app.providers.arsenkin import SE_LABELS  # noqa: E402
from app.services.goals import goal_names, page_key, parse_goal_ids  # noqa: E402
from app.services.not_found import DEFAULT_MARKERS, parse_markers  # noqa: E402

LEVELS = ("visits", "goals", "index", "notfound", "serp")
_CH = {"ad": "Реклама", "organic": "Поиск", "direct": "Прямые"}


def _channel(ts) -> str:
    return _CH.get((ts or "").lower(), "Прочее")


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", str(name)).strip("_") or "host"


def _sites(db):
    """site_id -> property_uri (для колонки «Сайт»)."""
    return {s.id: s.property_uri for s in db.execute(select(Site)).scalars()}


# ---------- уровни ----------
def _day(d, daily):
    return {"Дата": d.isoformat()} if daily else {}


def lvl_visits(db, d1, d2, sites, daily=False) -> pd.DataFrame:
    gcols = [Visit.site_id, Visit.start_url] + ([Visit.date] if daily else []) + [Visit.traffic_source]
    rows = db.execute(
        select(*gcols, func.count().label("v"), func.sum(Visit.duration).label("dur"),
               func.sum(Visit.bounce).label("bnc"))
        .where(Visit.date >= d1, Visit.date <= d2, Visit.start_url.isnot(None))
        .group_by(*gcols)).all()
    acc: dict = {}
    for r in rows:
        sid, url = r[0], r[1]
        d = r[2] if daily else None
        ts, v, dur, bnc = r[-4], r[-3], r[-2], r[-1]
        a = acc.setdefault((sid, url, d), {"v": 0, "dur": 0, "bnc": 0,
                                           "Поиск": 0, "Реклама": 0, "Прямые": 0, "Прочее": 0})
        a["v"] += v
        a["dur"] += int(dur or 0)
        a["bnc"] += int(bnc or 0)
        a[_channel(ts)] += v
    out = []
    for (sid, url, d), a in acc.items():
        v = a["v"]
        out.append({"Сайт": sites.get(sid, sid), "URL входа": url, **_day(d, daily), "Визиты": v,
                    "Ср. время (сек)": round(a["dur"] / v, 1) if v else 0,
                    "Отказы %": round(a["bnc"] / v * 100, 1) if v else 0,
                    "Поиск": a["Поиск"], "Реклама": a["Реклама"],
                    "Прямые": a["Прямые"], "Прочее": a["Прочее"]})
    df = pd.DataFrame(out)
    return df.sort_values("Визиты", ascending=False) if not df.empty else df


def lvl_goals(db, d1, d2, sites, daily=False) -> pd.DataFrame:
    rows = db.execute(
        select(Visit.site_id, Visit.start_url, Visit.date, Visit.extra).where(
            Visit.date >= d1, Visit.date <= d2, Visit.start_url.isnot(None),
            Visit.extra.isnot(None), Visit.extra.like("%goalsID%"))).all()
    names = goal_names(db, list({r[0] for r in rows})) if rows else {}
    acc: dict = {}
    for sid, url, d, extra in rows:
        key_d = d if daily else None
        for g in parse_goal_ids(extra):
            acc[(sid, url, key_d, g)] = acc.get((sid, url, key_d, g), 0) + 1
    out = [{"Сайт": sites.get(sid, sid), "URL входа": url, **_day(d, daily), "ID цели": g,
            "Название цели": names.get(g, f"Цель {g}"), "Достижений": c}
           for (sid, url, d, g), c in acc.items()]
    df = pd.DataFrame(out)
    return df.sort_values("Достижений", ascending=False) if not df.empty else df


def lvl_index(db, d1, d2, sites, daily=False) -> pd.DataFrame:
    # по умолчанию — последний снимок по сайту; --daily = все снимки в периоде
    if daily:
        rows = db.execute(select(IndexedUrlSnapshot.site_id, IndexedUrlSnapshot.captured_on,
                                 IndexedUrlSnapshot.url, IndexedUrlSnapshot.title)
                          .where(IndexedUrlSnapshot.captured_on >= d1,
                                 IndexedUrlSnapshot.captured_on <= d2)).all()
    else:
        latest = dict(db.execute(select(IndexedUrlSnapshot.site_id,
                                        func.max(IndexedUrlSnapshot.captured_on))
                                 .group_by(IndexedUrlSnapshot.site_id)).all())
        rows = []
        for sid, cap in latest.items():
            for u, t in db.execute(select(IndexedUrlSnapshot.url, IndexedUrlSnapshot.title)
                                   .where(IndexedUrlSnapshot.site_id == sid,
                                          IndexedUrlSnapshot.captured_on == cap)).all():
                rows.append((sid, cap, u, t))
    out = [{"Сайт": sites.get(sid, sid), "URL": u, "Заголовок": t or "",
            "Дата снимка": cap.isoformat()} for sid, cap, u, t in rows]
    return pd.DataFrame(out)


def lvl_notfound(db, d1, d2, sites, markers, daily=False) -> pd.DataFrame:
    cond = or_(*[Hit.title.ilike(f"%{m}%") for m in markers])
    gcols = [Hit.site_id, Hit.url] + ([Hit.date] if daily else []) + [Hit.traffic_source]
    rows = db.execute(select(*gcols, func.count().label("n"))
                      .where(Hit.date >= d1, Hit.date <= d2, Hit.title.isnot(None), cond)
                      .group_by(*gcols)).all()
    acc: dict = {}
    for r in rows:
        sid, url = r[0], r[1]
        d = r[2] if daily else None
        ts, n = r[-2], r[-1]
        a = acc.setdefault((sid, url, d), {"n": 0, "Поиск": 0, "Реклама": 0, "Прямые": 0, "Прочее": 0})
        a["n"] += n
        a[_channel(ts)] += n
    out = [{"Сайт": sites.get(sid, sid), "URL (битый)": url, **_day(d, daily), "Просмотры": a["n"],
            "Поиск": a["Поиск"], "Реклама": a["Реклама"],
            "Прямые": a["Прямые"], "Прочее": a["Прочее"]}
           for (sid, url, d), a in acc.items()]
    df = pd.DataFrame(out)
    return df.sort_values("Просмотры", ascending=False) if not df.empty else df


def lvl_serp(db, d1, d2, sites, daily=False) -> pd.DataFrame:
    # по умолчанию — последний срез в периоде; --daily = все срезы периода
    if daily:
        rows = db.execute(select(SerpResult).where(SerpResult.captured_on >= d1,
                                                    SerpResult.captured_on <= d2)
                          .order_by(SerpResult.captured_on, SerpResult.keyword,
                                    SerpResult.se, SerpResult.position)).scalars().all()
    else:
        cap = db.execute(select(func.max(SerpResult.captured_on))
                         .where(SerpResult.captured_on <= d2)).scalar()
        if cap is None:
            return pd.DataFrame()
        rows = db.execute(select(SerpResult).where(SerpResult.captured_on == cap)
                          .order_by(SerpResult.keyword, SerpResult.se, SerpResult.position)).scalars().all()
    out = [{"Запрос": r.keyword, "ПС": SE_LABELS.get(r.se, r.se), "Регион": r.region,
            "Позиция": r.position, "URL": r.url or "", "Домен": r.url_domain or "",
            "Заголовок": r.title or "", "Дата": r.captured_on.isoformat()} for r in rows]
    return pd.DataFrame(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--end-of-week", dest="end_of_week", action="store_true")
    ap.add_argument("--levels", default="visits,goals,index,notfound,serp")
    ap.add_argument("--per-host", dest="per_host", action="store_true")
    ap.add_argument("--markers", default=None, help="маркеры заголовка 404 через запятую")
    ap.add_argument("--daily", action="store_true",
                    help="по дням (визиты/цели/404 с колонкой «Дата»; индекс/SERP — все "
                         "срезы периода вместо последнего) — файлы кратно больше")
    a = ap.parse_args()

    today = datetime.date.today()
    if a.end:
        end = datetime.date.fromisoformat(a.end)
    elif a.end_of_week:
        end = today - datetime.timedelta(days=today.weekday() + 1)
    else:
        end = today - datetime.timedelta(days=1)
    start = datetime.date.fromisoformat(a.start) if a.start else end - datetime.timedelta(days=a.days - 1)
    levels = [x.strip() for x in a.levels.split(",") if x.strip() in LEVELS]
    if not levels:
        sys.exit(f"Нет валидных уровней. Доступны: {', '.join(LEVELS)}")
    if not os.path.isdir(a.out):
        sys.exit(f"Папка не найдена: {a.out}")
    markers = parse_markers(a.markers) if a.markers else list(DEFAULT_MARKERS)
    tag = f"{start.isoformat()}_{end.isoformat()}" + ("_daily" if a.daily else "")

    init_db()
    db = SessionLocal()
    try:
        sites = _sites(db)
        print(f"Период {start}…{end}, уровни: {', '.join(levels)}\nПапка: {a.out}\n")
        for level in levels:
            if level == "visits":
                df = lvl_visits(db, start, end, sites, a.daily)
            elif level == "goals":
                df = lvl_goals(db, start, end, sites, a.daily)
            elif level == "index":
                df = lvl_index(db, start, end, sites, a.daily)
            elif level == "notfound":
                df = lvl_notfound(db, start, end, sites, markers, a.daily)
            else:
                df = lvl_serp(db, start, end, sites, a.daily)
            if df is None or df.empty:
                print(f"  {level:8} — данных нет, пропуск")
                continue
            if a.per_host and "Сайт" in df.columns:
                for host, sub in df.groupby("Сайт"):
                    path = os.path.join(a.out, f"extra_{level}_{_safe(host)}_{tag}.csv")
                    sub.to_csv(path, sep=";", index=False, encoding="utf-8-sig")
                    print(f"  {level:8} · {host} → {len(sub)} строк")
            else:
                path = os.path.join(a.out, f"extra_{level}_{tag}.csv")
                df.to_csv(path, sep=";", index=False, encoding="utf-8-sig")
                print(f"  {level:8} → {len(df)} строк  ({os.path.basename(path)})")
        print("\nГотово.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
