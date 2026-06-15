"""Read/export the stored ARSENKIN ТОП-10 results (``serp_result``)."""
from __future__ import annotations

import io

import pandas as pd
from sqlalchemy import func, select

from app.db.models import SerpResult
from app.providers.arsenkin import SE_LABELS

CSV_MEDIA = "text/csv"
XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def captures(db) -> list[str]:
    """Available capture dates, newest first."""
    rows = db.execute(
        select(SerpResult.captured_on).distinct().order_by(SerpResult.captured_on.desc())
    ).all()
    return [d.isoformat() for (d,) in rows if d is not None]


def serp_rows(db, captured_on: str | None = None, se=None, domain: str | None = None,
              search: str | None = None, limit: int | None = None) -> list[dict]:
    cap = captured_on
    if not cap:
        d = db.execute(select(func.max(SerpResult.captured_on))).scalar()
        cap = d.isoformat() if d else None
    if not cap:
        return []
    stmt = select(SerpResult).where(SerpResult.captured_on == cap)
    if se:
        stmt = stmt.where(SerpResult.se.in_(list(se)))
    if domain:
        stmt = stmt.where(SerpResult.url_domain == domain)
    if search:
        stmt = stmt.where(SerpResult.keyword.ilike(f"%{search}%"))
    stmt = stmt.order_by(SerpResult.keyword, SerpResult.se, SerpResult.position)
    if limit:
        stmt = stmt.limit(limit)
    out = []
    for r in db.execute(stmt).scalars().all():
        out.append({"keyword": r.keyword, "se": r.se, "se_label": SE_LABELS.get(r.se, r.se),
                    "region": r.region, "position": r.position, "url": r.url,
                    "url_domain": r.url_domain, "title": r.title, "snippet": r.snippet,
                    "captured_on": r.captured_on.isoformat() if r.captured_on else ""})
    return out


def own_positions(db, captured_on: str | None, own_domains, se=None) -> list[dict]:
    """For our own domains in a capture: per (domain, ПС) — in how many keywords
    it ranks, how many in top-3 / top-10, and the average position (best position
    per keyword)."""
    cap = captured_on
    if not cap:
        d = db.execute(select(func.max(SerpResult.captured_on))).scalar()
        cap = d.isoformat() if d else None
    if not cap or not own_domains:
        return []
    stmt = select(SerpResult).where(SerpResult.captured_on == cap,
                                    SerpResult.url_domain.in_(list(own_domains)))
    if se:
        stmt = stmt.where(SerpResult.se.in_(list(se)))
    best: dict = {}  # (domain, se) -> {keyword: best position}
    for r in db.execute(stmt).scalars():
        kws = best.setdefault((r.url_domain, r.se), {})
        if r.keyword not in kws or r.position < kws[r.keyword]:
            kws[r.keyword] = r.position
    out = []
    for (dom, se_t), kws in best.items():
        poss = list(kws.values())
        out.append({"domain": dom, "se": se_t, "se_label": SE_LABELS.get(se_t, se_t),
                    "keywords": len(poss),
                    "top3": sum(1 for p in poss if p <= 3),
                    "top10": sum(1 for p in poss if p <= 10),
                    "avg": round(sum(poss) / len(poss), 1) if poss else 0})
    out.sort(key=lambda r: r["keywords"], reverse=True)
    return out


def build_serp_export(rows: list[dict], dr_label: str, fmt: str = "csv"):
    df = pd.DataFrame(
        [{"Запрос": r["keyword"], "ПС": r["se_label"], "Регион": r["region"],
          "Позиция": r["position"], "URL": r["url"], "Домен": r["url_domain"],
          "Заголовок": r["title"], "Сниппет": r.get("snippet"),
          "Дата": r["captured_on"]} for r in rows],
        columns=["Запрос", "ПС", "Регион", "Позиция", "URL", "Домен", "Заголовок", "Сниппет", "Дата"],
    )
    safe = "".join(c if (c.isascii() and c.isalnum()) else "_" for c in (dr_label or "all"))[:30]
    name = f"serp_top_{safe.strip('_') or 'all'}_{dr_label or ''}"
    buf = io.BytesIO()
    if fmt == "csv":
        buf.write(df.to_csv(index=False).encode("utf-8-sig"))
        buf.seek(0)
        return f"{name}.csv", buf, CSV_MEDIA
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="ТОП-10", index=False)
    buf.seek(0)
    return f"{name}.xlsx", buf, XLSX_MEDIA
