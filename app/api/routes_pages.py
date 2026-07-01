"""Server-rendered HTML pages (Jinja2 + Chart.js)."""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import CollectionRun, Project, ProjectUrl, Site, Source
from app.deps import get_db, parse_date_range, resolve_period_b
from app.services import growth
from app.services import totals as totals_svc
from app.services.ctr import ctr_for_project
from app.services.loaders import project_page_ids
from app.services.top_keyword import top_keywords_for_project
from app.utils import domain_of, is_tracking_url, normalize_url
from app.web import templates

router = APIRouter(tags=["pages"], include_in_schema=False)

BP = get_settings().base_path  # "" or e.g. "/stat" — for redirect targets

# Recorded Webvisor replays (written by scripts/webvisor.py --record).
WEBVISOR_VIDEOS = Path(os.environ.get(
    "WEBVISOR_OUT", str(Path(__file__).resolve().parents[2] / "data" / "webvisor" / "videos")))
_WEBM_RE = re.compile(r"^[A-Za-z0-9_-]+\.webm$")  # safe filename, blocks path traversal


def _wv_phrases() -> dict:
    """visit_id -> search phrase, harvested into data/webvisor/sessions.jsonl."""
    out, f = {}, WEBVISOR_VIDEOS.parent / "sessions.jsonl"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("visit_id") and d.get("phrase"):
                out[str(d["visit_id"])] = d["phrase"]
    return out


def _wv_roistat(start_url, extra):
    """Roistat id from the landing URL (?roistat=…) or a Metrica param in ``extra``."""
    if start_url and "roistat" in start_url.lower():
        try:
            qs = parse_qs(urlparse(start_url).query)
            for k in ("roistat", "rs", "roistat_visit"):
                if qs.get(k):
                    return qs[k][0]
        except Exception:
            pass
    if extra:
        try:
            for k, v in json.loads(extra).items():
                if "roistat" in k.lower():
                    return str(v)
        except Exception:
            pass
    return None


def _wv_utm(extra) -> dict:
    """UTM tags (and any *utm* field) preserved in the visit's ``extra`` JSON."""
    out = {}
    if extra:
        try:
            for k, v in json.loads(extra).items():
                if "utm" in k.lower() and v:
                    out[k] = v
        except Exception:
            pass
    return out


def _public_redirect_uri(request: Request) -> str:
    """The exact OAuth callback URL to register in Google (must match)."""
    base = get_settings().public_base_url.strip().rstrip("/")
    if base:
        return f"{base}{BP}/oauth/callback"
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}{BP}/oauth/callback"


def _sites(db: Session) -> list[Site]:
    return db.execute(select(Site).order_by(Site.id)).scalars().all()


def _resolve_site(db: Session, site_id: int | None) -> Site | None:
    if site_id is not None:
        return db.get(Site, site_id)
    return db.execute(select(Site).order_by(Site.id).limit(1)).scalar_one_or_none()


def _same_domain_ids(db: Session, site: Site) -> list[int]:
    """site_ids of the SAME source sharing the bare domain — e.g. a GSC domain
    property (sc-domain:) and its https:// URL-prefix property of one site."""
    d = domain_of(site.property_uri)
    rows = db.execute(
        select(Site.id, Site.property_uri).where(Site.source_id == site.source_id)
    ).all()
    ids = [sid for sid, uri in rows if d and domain_of(uri) == d]
    return ids or [site.id]


# Search engines shown as pickable channels on the dashboard (Metrica is visits,
# not search clicks, so it's excluded here).
SEARCH_ENGINES = ("gsc", "yandex_webmaster")


def _domains(db: Session) -> list[dict]:
    """Distinct bare domains across GSC/Yandex sites + which engines each has.
    https:// and sc-domain: of one domain collapse into a single entry."""
    rows = db.execute(
        select(Source.code, Site.property_uri).join(Site, Site.source_id == Source.id)
    ).all()
    m: dict[str, set] = {}
    for code, uri in rows:
        if code in SEARCH_ENGINES:
            d = domain_of(uri)
            if d:
                m.setdefault(d, set()).add(code)
    return [{"domain": d, "engines": sorted(m[d])} for d in sorted(m)]


def _keyword_site_ids(db: Session, domain: str | None, engine_codes) -> list[int]:
    """site_ids for the keyword view: one domain's properties, or — when
    ``domain`` is "" (all) — every search-engine property."""
    if domain:
        return [sid for ids in _domain_engine_ids(db, domain, engine_codes).values()
                for sid in ids]
    rows = db.execute(
        select(Site.id, Source.code).join(Source, Site.source_id == Source.id)
    ).all()
    return [sid for sid, code in rows if code in engine_codes]


def _domain_engine_ids(db: Session, domain: str, engine_codes) -> dict[str, list[int]]:
    """{engine_code: [site_ids]} for one domain — every same-host property of that
    engine (so https:// + sc-domain: are merged downstream)."""
    rows = db.execute(
        select(Site.id, Site.property_uri, Source.code).join(Source, Site.source_id == Source.id)
    ).all()
    out: dict[str, list[int]] = {}
    for sid, uri, code in rows:
        if code in engine_codes and domain_of(uri) == domain:
            out.setdefault(code, []).append(sid)
    return out


def _clean_combined(db, engine_ids, dr, ratio, min_impr) -> dict:
    """Bot-cleaned totals: de-dup per-URL within an engine (max impressions),
    then sum across engines."""
    from app.services.antifraud import clean_values_by_url

    by: dict[str, dict] = {}
    for ids in engine_ids.values():
        merged: dict[str, dict] = {}
        for sid in ids:
            for url, v in clean_values_by_url(
                db, sid, dr, ratio_threshold=ratio, min_impressions=min_impr
            ).items():
                cur = merged.get(url)
                if cur is None or v["impressions"] > cur["impressions"]:
                    merged[url] = v
        for url, v in merged.items():
            b = by.setdefault(url, {"clicks": 0, "impressions": 0, "pw": 0.0})
            b["clicks"] += v["clicks"]
            b["impressions"] += v["impressions"]
            b["pw"] += v["position"] * v["impressions"]
    cl = sum(b["clicks"] for b in by.values())
    im = sum(b["impressions"] for b in by.values())
    pw = sum(b["pw"] for b in by.values())
    return {"clicks": cl, "impressions": im, "ctr": (cl / im) if im else 0.0,
            "position": (pw / im) if im else 0.0}


@router.get("/")
def dashboard(request: Request, domain: str | None = None,
              engines: list[str] | None = Query(None), start: str | None = None,
              end: str | None = None, msg: str | None = None, clean: int = 0,
              ratio: float = 10.0, min_impr: int = 100, devices: int = 0,
              show_tagged: int = 0, gran: str = "day", db: Session = Depends(get_db)):
    domains = _domains(db)
    cur = domain if domain and any(d["domain"] == domain for d in domains) \
        else (domains[0]["domain"] if domains else None)
    avail = next((d["engines"] for d in domains if d["domain"] == cur), [])
    sel = [e for e in (engines or avail) if e in avail] or avail  # default: all the domain has
    dr = parse_date_range(start, end)
    ctx = {
        "request": request, "msg": msg, "domains": domains, "cur_domain": cur,
        "engines": sel, "range": dr, "gran": gran,
        "projects": db.execute(select(Project).order_by(Project.id)).scalars().all(),
        "clean": bool(clean), "ratio": ratio, "min_impr": min_impr, "devices": bool(devices),
        "totals": None, "daily": [], "top_pages": [], "export_sites": [], "site_ids": [],
        "hidden_tagged": 0, "show_tagged": bool(show_tagged),
        "tagged_series": [], "clean_series": [], "tagged_clicks_total": 0,
        "chart_total_clicks": 0, "tagged_impr_total": 0,
        "runs": db.execute(
            select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(10)
        ).scalars().all(),
        "gsc_site_url": get_settings().gsc_site_url,
    }
    if cur and sel:
        engine_ids = _domain_engine_ids(db, cur, sel)  # {code: [site_ids]}
        parts = list(engine_ids.values())
        labels = {"gsc": "Google", "yandex_webmaster": "Яндекс"}
        ctx["export_sites"] = [{"id": sid, "label": labels.get(code, code)}
                               for code, ids in engine_ids.items() for sid in ids]
        ctx["site_ids"] = [s["id"] for s in ctx["export_sites"]]
        daily = totals_svc.combine_daily([totals_svc.site_daily(db, ids, dr) for ids in parts])
        ctx["daily"] = totals_svc.bucket_series(daily, gran)
        # separate line: clicks from ad/tracking-tagged URLs (utm_/roistat/{macros}),
        # aligned to the same buckets as the main site-total chart
        tagged = totals_svc.bucket_series(
            totals_svc.combine_daily([totals_svc.tagged_daily(db, ids, dr) for ids in parts]), gran)
        tmap = {t["date"]: t["clicks"] for t in tagged}
        ctx["tagged_series"] = [tmap.get(p["date"], 0) for p in ctx["daily"]]
        # clean line = source total minus tagged clicks per bucket (undistorted organic trend)
        ctx["clean_series"] = [max(0, p["clicks"] - t)
                               for p, t in zip(ctx["daily"], ctx["tagged_series"])]
        ctx["tagged_clicks_total"] = sum(t["clicks"] for t in tagged)
        ctx["chart_total_clicks"] = sum(p["clicks"] for p in ctx["daily"])  # тотал источника на графике
        ctx["tagged_impr_total"] = sum(t["impressions"] for t in tagged)
        if devices:
            merged = totals_svc.combine_pages_devices(
                [totals_svc.per_page_with_devices(db, ids, dr) for ids in parts], limit=10**9)
        else:
            merged = totals_svc.combine_pages(
                [totals_svc.per_page_totals(db, ids, dr) for ids in parts], limit=10**9)
        # drop advertising/tracking-tagged URLs (utm_/roistat/{macros}) that leaked
        # into the organic index — they're noise in per-page search stats
        ctx["hidden_tagged"] = sum(1 for p in merged if is_tracking_url(p["url"]))
        if not show_tagged:
            merged = [p for p in merged if not is_tracking_url(p["url"])]
        ctx["top_pages"] = merged[:20]
        if clean:
            ctx["totals"] = _clean_combined(db, engine_ids, dr, ratio, min_impr)
        else:
            ctx["totals"] = totals_svc.combine_totals(
                [totals_svc.site_totals(db, ids, dr) for ids in parts])
    return templates.TemplateResponse(request, "dashboard.html", ctx)


def _kw_resolve(db, domain, engines):
    """(domains, cur, selected_engines, site_ids) for the keyword views.
    ``domain`` None -> first domain; "" -> all domains."""
    domains = _domains(db)
    cur = (domains[0]["domain"] if domains else None) if domain is None else domain
    avail = (list(SEARCH_ENGINES) if cur == "" else
             next((d["engines"] for d in domains if d["domain"] == cur), []))
    sel = [e for e in (engines or avail) if e in avail] or avail
    ids = _keyword_site_ids(db, cur, sel) if cur is not None and sel else []
    return domains, cur, sel, ids


@router.get("/keywords")
def keywords_page(request: Request, domain: str | None = None,
                  engines: list[str] | None = Query(None), start: str | None = None,
                  end: str | None = None, q: str | None = None,
                  min_clicks: int = 0, min_impr: int = 0, db: Session = Depends(get_db)):
    from app.services import keywords as kw

    domains, cur, sel, ids = _kw_resolve(db, domain, engines)
    dr = parse_date_range(start, end)
    rows, summary = [], None
    if ids:
        summary = kw.keyword_summary(db, ids, dr)
        rows = kw.keyword_rows(db, ids, dr, min_clicks=min_clicks, min_impr=min_impr,
                               search=(q or None), limit=2000)
    return templates.TemplateResponse(request, "keywords.html", {
        "request": request, "domains": domains, "cur_domain": cur, "engines": sel,
        "range": dr, "rows": rows, "summary": summary, "shown_limit": 2000,
        "q": q or "", "min_clicks": min_clicks, "min_impr": min_impr,
    })


@router.get("/keywords/export")
def keywords_export(domain: str | None = None, engines: list[str] | None = Query(None),
                    start: str | None = None, end: str | None = None, q: str | None = None,
                    min_clicks: int = 0, min_impr: int = 0, format: str = "csv",
                    db: Session = Depends(get_db)):
    from app.services import keywords as kw

    _, cur, _, ids = _kw_resolve(db, domain, engines)
    dr = parse_date_range(start, end)
    rows = kw.keyword_rows(db, ids, dr, min_clicks=min_clicks, min_impr=min_impr,
                           search=(q or None), limit=None)  # ALL keywords
    fmt = "csv" if format == "csv" else "xlsx"
    fn, buf, media = kw.build_keyword_export(rows, cur or "all", dr, fmt)
    return StreamingResponse(buf, media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@router.get("/serp")
def serp_page(request: Request, captured_on: str | None = None,
              se: list[int] | None = Query(None), domain: str | None = None,
              q: str | None = None, max_pos: str | None = None, msg: str | None = None,
              db: Session = Depends(get_db)):
    from app.credentials import get_cred
    from app.services import serp as S
    from app.services.serp_run import current_status, pending_count

    caps = S.captures(db)
    cap = captured_on or (caps[0] if caps else None)
    se_sel = list(se) if se else []
    mp = _qint(max_pos)
    kw_filter = _parse_phrases(get_cred("serp_kw_filter") or "")
    rows = S.serp_rows(db, captured_on=cap, se=(se_sel or None), domain=(domain or None),
                       search=(q or None), limit=2000, max_position=mp,
                       keywords=(kw_filter or None))
    own = {d["domain"] for d in _domains(db)}
    return templates.TemplateResponse(request, "serp.html", {
        "request": request, "captures": caps, "cap": cap, "rows": rows,
        "se_sel": se_sel, "domain": domain or "", "q": q or "", "msg": msg,
        "max_pos": mp or "", "kw_filter_n": len(kw_filter),
        "domains": _domains(db), "own_domains": own, "shown_limit": 2000,
        "own_summary": S.own_positions(db, cap, own, se=(se_sel or None)),
        "status": current_status(), "has_token": bool(get_cred("arsenkin_token")),
        "pending": pending_count(db),
    })


def _parse_phrases(text: str) -> list[str]:
    seen, out = set(), []
    for line in (text or "").replace("\t", "\n").replace(",", "\n").splitlines():
        p = line.strip().strip('"')
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


@router.post("/ui/serp/run")
async def ui_serp_run(domain: str = Form(""), se: list[int] = Form(default=[]),
                      yandex_region: int = Form(213), google_region: int = Form(1011969),
                      depth: int = Form(10), min_clicks: int = Form(1),
                      max_keywords: int = Form(500), period_days: int = Form(90),
                      batch: int = Form(1000), snippets: int = Form(1), phrases: str = Form(""),
                      file: UploadFile | None = File(None), db: Session = Depends(get_db)):
    from datetime import timedelta

    from app.credentials import get_cred
    from app.providers.arsenkin import se_for
    from app.services import keywords as kw
    from app.services import serp_run

    def back(m: str):
        return RedirectResponse(url=f"{BP}/serp?msg={quote(m)}", status_code=303)

    token = get_cred("arsenkin_token")
    if not token:
        return back("Сначала вставьте токен arsenkin в Настройках.")
    if serp_run.current_status().get("running"):
        return back("Проверка уже идёт — дождитесь завершения.")
    se_list = se_for(se or [2, 11], yandex_region, google_region)

    # source of phrases: your own (textarea/file) wins; otherwise the collected keywords
    words = _parse_phrases(phrases)
    if file is not None and file.filename:
        try:
            words += _parse_phrases((await file.read()).decode("utf-8", "ignore"))
        finally:
            await file.close()
    src = "свои фразы"
    if words:
        seen, uniq = set(), []
        for w in words:
            if w.lower() not in seen:
                seen.add(w.lower())
                uniq.append(w)
        words = uniq[:max_keywords] if max_keywords else uniq
    else:
        end = date.today() - timedelta(days=1)
        dr = parse_date_range((end - timedelta(days=max(1, period_days) - 1)).isoformat(),
                              end.isoformat())
        ids = _keyword_site_ids(db, domain or "", SEARCH_ENGINES)
        words = [r["query"] for r in kw.keyword_rows(db, ids, dr, min_clicks=min_clicks,
                                                     limit=(max_keywords or None)) if r["query"]]
        src = "ключевые слова из статистики"
    if not words:
        return back("Не нашлось фраз: вставь свои или ослабь фильтр «мин. кликов».")
    serp_run.launch_run(words, se_list, token=token, depth=depth, snippets=bool(snippets),
                        batch=max(1, min(batch, 5000)), timeout_min=120)
    return back(f"Запущено ({src}): {len(words)} фраз × {len(se_list)} ПС "
                f"(~{len(words) * len(se_list)} лимитов). Обновляйте страницу — результаты появятся.")


@router.post("/ui/serp/fetch_pending")
def ui_serp_fetch_pending(db: Session = Depends(get_db)):
    from app.credentials import get_cred
    from app.services import serp_run

    def back(m: str):
        return RedirectResponse(url=f"{BP}/serp?msg={quote(m)}", status_code=303)

    token = get_cred("arsenkin_token")
    if not token:
        return back("Сначала вставьте токен arsenkin в Настройках.")
    if serp_run.current_status().get("running"):
        return back("Идёт другой прогон — дождитесь завершения.")
    if serp_run.pending_count(db) == 0:
        return back("Недостающих задач нет — всё уже загружено.")
    serp_run.launch_fetch_pending(token=token)
    return back("Догрузка недостающих задач запущена. Обновляйте страницу.")


@router.post("/ui/serp/urls")
async def ui_serp_urls(urls: str = Form(""), clear: int = Form(0),
                       file: UploadFile | None = File(None), db: Session = Depends(get_db)):
    """Upload URLs -> their keywords (from our collected base) become a filter for
    the stored SERP results (no arsenkin run). Saved + applied to the view/export."""
    from datetime import date as _date

    from app.credentials import set_cred
    from app.services import keywords as kw

    def back(m: str):
        return RedirectResponse(url=f"{BP}/serp?msg={quote(m)}", status_code=303)

    if clear:
        set_cred("serp_kw_filter", "")
        return back("Фильтр по ссылкам снят.")
    url_list = _parse_phrases(urls)
    if file is not None and getattr(file, "filename", ""):
        try:
            url_list += _parse_phrases((await file.read()).decode("utf-8", "ignore"))
        finally:
            await file.close()
    if not url_list:
        set_cred("serp_kw_filter", "")
        return back("Список ссылок пуст — фильтр снят.")
    ids = _keyword_site_ids(db, "", SEARCH_ENGINES)
    dr = parse_date_range("2000-01-01", _date.today().isoformat())  # all collected history
    words = kw.keywords_for_urls(db, ids, url_list, dr)
    set_cred("serp_kw_filter", "\n".join(words))
    return back(f"По {len(url_list)} ссылкам найдено {len(words)} ключей в базе — "
                "ставь «Поз. ≤ 3» для топ-3."
                if words else "По этим ссылкам ключей в базе нет (URL не совпали со страницами).")


@router.get("/serp/export")
def serp_export(captured_on: str | None = None, se: list[int] | None = Query(None),
                domain: str | None = None, q: str | None = None, max_pos: str | None = None,
                format: str = "csv", db: Session = Depends(get_db)):
    from app.credentials import get_cred
    from app.services import serp as S

    caps = S.captures(db)
    cap = captured_on or (caps[0] if caps else None)
    kw_filter = _parse_phrases(get_cred("serp_kw_filter") or "")
    rows = S.serp_rows(db, captured_on=cap, se=(list(se) if se else None),
                       domain=(domain or None), search=(q or None), limit=None,
                       max_position=_qint(max_pos), keywords=(kw_filter or None))
    fmt = "csv" if format == "csv" else "xlsx"
    fn, buf, media = S.build_serp_export(rows, cap or "all", fmt)
    return StreamingResponse(buf, media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@router.get("/upload")
def upload_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "upload.html",
        {
            "request": request,
            "sites": _sites(db),
            "projects": db.execute(select(Project).order_by(Project.id)).scalars().all(),
        },
    )


@router.post("/ui/projects")
def ui_create_project(name: str = Form(...), site_id: int = Form(...),
                      db: Session = Depends(get_db)):
    if db.get(Site, site_id) is None:
        raise HTTPException(404, "site not found")
    project = Project(name=name, site_id=site_id)
    db.add(project)
    db.commit()
    return RedirectResponse(url=f"{BP}/projects/{project.id}", status_code=303)


@router.post("/ui/projects/{project_id}/urls")
async def ui_add_urls(project_id: int, urls_text: str | None = Form(None),
                      file: UploadFile | None = File(None), db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    raw = ""
    if file is not None:
        raw += (await file.read()).decode("utf-8", errors="ignore") + "\n"
    if urls_text:
        raw += urls_text
    existing = {u.normalized_url for u in project.urls}
    for line in raw.replace(",", "\n").splitlines():
        url = line.strip()
        if not url:
            continue
        norm = normalize_url(url)
        if norm and norm not in existing:
            db.add(ProjectUrl(project_id=project.id, url=url, normalized_url=norm))
            existing.add(norm)
    db.commit()
    return RedirectResponse(url=f"{BP}/projects/{project_id}", status_code=303)


@router.post("/ui/collect")
def ui_collect(site_id: list[int] = Form(...), domain: str = Form(""),
               engines: list[str] = Form([]), db: Session = Depends(get_db)):
    from app.scheduler.jobs import collect_site, compute_window

    for sid in site_id:  # a domain can span several properties / engines
        site = db.get(Site, sid)
        if site is None:
            continue
        dr = compute_window(db, site, date.today(), get_settings().collect_refetch_days)
        try:
            collect_site(db, site, dr)
        except Exception:  # noqa: BLE001 - surfaced via /api/admin/status
            pass
    qs = f"?domain={quote(domain)}" if domain else ""
    for e in engines:
        qs += ("&" if qs else "?") + f"engines={quote(e)}"
    return RedirectResponse(url=f"{BP}/{qs}", status_code=303)


@router.get("/admin")
def admin_page(request: Request, msg: str | None = None, db: Session = Depends(get_db)):
    from app.cache import stats as cache_stats
    from app.credentials import get_cred
    from app.scheduler.jobs import last_daily_ok as _last_daily_ok
    from app.scheduler.jobs import scheduler_status as _scheduler_status
    from app.scheduler.jobs import stale_site_ids as _stale_site_ids

    s = get_settings()
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "request": request,
            "msg": msg,
            "gsc_mode": get_cred("gsc_auth_mode"),
            "yandex_connected": bool(get_cred("yandex_wm_token")),
            "arsenkin_connected": bool(get_cred("arsenkin_token")),
            "miralinks_connected": bool(get_cred("miralinks_cookie")),
            "miralinks_body_set": bool(get_cred("miralinks_body")),
            "oauth_redirect_uri": _public_redirect_uri(request),
            "sites": _sites(db),
            "sources": db.execute(select(Source).order_by(Source.id)).scalars().all(),
            "runs": db.execute(
                select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(30)
            ).scalars().all(),
            "config": {
                "gsc_auth_mode": s.gsc_auth_mode,
                "gsc_site_url": s.gsc_site_url or "—",
                "enable_scheduler": s.enable_scheduler,
                "collect_cron_hour": s.collect_cron_hour,
                "collect_refetch_days": s.collect_refetch_days,
                "page_cache_ttl": s.page_cache_ttl,
            },
            "cache_stats": cache_stats(),
            "scheduler": _scheduler_status(),
            "last_daily": _last_daily_ok(db),
            "stale_sites": len(_stale_site_ids(db)),
        },
    )


@router.get("/ui/admin/device-cov")
def ui_admin_device_cov(db: Session = Depends(get_db)):
    """Per-site device-metric coverage (date span + row count). Loaded async by the
    admin page so it renders instantly — the GROUP BY over device_metric_daily is
    heavy on a large DB."""
    from sqlalchemy import func

    from app.db.models import DeviceMetricDaily

    rows = db.execute(
        select(
            DeviceMetricDaily.site_id,
            func.min(DeviceMetricDaily.date),
            func.max(DeviceMetricDaily.date),
            func.count(),
        ).group_by(DeviceMetricDaily.site_id)
    ).all()
    return {str(sid): (f"{lo}..{hi} ({c})" if c else "—") for sid, lo, hi, c in rows}


@router.post("/ui/sites")
def ui_create_site(property_uri: str = Form(...), source_code: str = Form("gsc"),
                   display_name: str | None = Form(None),
                   external_host_id: str | None = Form(None),
                   db: Session = Depends(get_db)):
    source = db.execute(select(Source).where(Source.code == source_code)).scalar_one_or_none()
    if source is None:
        raise HTTPException(400, "unknown source")
    site = Site(
        source_id=source.id,
        property_uri=property_uri.strip(),
        display_name=(display_name or property_uri).strip(),
        external_host_id=(external_host_id.strip() or None) if external_host_id else None,
        enabled=True,
    )
    db.add(site)
    db.commit()
    return RedirectResponse(url=f"{BP}/?site_id={site.id}", status_code=303)


@router.post("/ui/sites/{site_id}/toggle")
def ui_toggle_site(site_id: int, db: Session = Depends(get_db)):
    site = db.get(Site, site_id)
    if site is not None:
        site.enabled = not site.enabled
        db.commit()
    return RedirectResponse(url=f"{BP}/admin", status_code=303)


@router.post("/ui/arsenkin/token")
def ui_arsenkin_token(token: str = Form(...)):
    from app.credentials import set_cred

    token = (token or "").strip()
    set_cred("arsenkin_token", token)
    msg = "Токен arsenkin сохранён." if token else "Токен arsenkin очищен."
    return RedirectResponse(url=f"{BP}/admin?msg={quote(msg)}", status_code=303)


@router.post("/ui/miralinks/save")
def ui_miralinks_save(cookie: str = Form(""), body: str = Form("")):
    from app.credentials import set_cred

    saved = []
    cookie = (cookie or "").strip()
    body = (body or "").strip()
    if cookie:
        set_cred("miralinks_cookie", cookie)
        saved.append("cookie")
    if body:
        set_cred("miralinks_body", body)
        saved.append("запрос")
    msg = ("Miralinks: сохранены " + " и ".join(saved) + ".") if saved \
        else "Miralinks: нечего сохранять (пустые поля)."
    return RedirectResponse(url=f"{BP}/admin?msg={quote(msg)}", status_code=303)


def _qint(v: str | None) -> int | None:
    """Query int that tolerates empty strings (blank HTML number inputs)."""
    try:
        return int(str(v).strip()) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


@router.get("/donors")
def donors_page(request: Request, q: str | None = None, region: str | None = None,
                topic: str | None = None, min_sqi: str | None = None,
                max_price: str | None = None, min_dr: str | None = None,
                min_traffic: str | None = None, sort: str = "sqi",
                msg: str | None = None, db: Session = Depends(get_db)):
    from app.credentials import get_cred
    from app.services import donors as D

    f = {"min_sqi": _qint(min_sqi), "max_price": _qint(max_price),
         "min_dr": _qint(min_dr), "min_traffic": _qint(min_traffic)}
    rows = D.donor_rows(db, q=q, region=region, topic=topic, sort=sort, limit=1000, **f)
    return templates.TemplateResponse(request, "donors.html", {
        "request": request, "rows": rows, "regions": D.regions(db),
        "total": D.count(db), "shown_limit": 1000, "msg": msg,
        "q": q or "", "region": region or "", "topic": topic or "",
        "min_sqi": f["min_sqi"] or "", "max_price": f["max_price"] or "",
        "min_dr": f["min_dr"] or "", "min_traffic": f["min_traffic"] or "", "sort": sort,
        "status": D.current_status(),
        "has_cookie": bool(get_cred("miralinks_cookie")),
        "has_body": bool(get_cred("miralinks_body")),
    })


@router.get("/donors/export")
def donors_export(q: str | None = None, region: str | None = None, topic: str | None = None,
                  min_sqi: str | None = None, max_price: str | None = None,
                  min_dr: str | None = None, min_traffic: str | None = None,
                  sort: str = "sqi", format: str = "csv", db: Session = Depends(get_db)):
    from app.services import donors as D

    rows = D.donor_rows(db, q=q, region=region, topic=topic, sort=sort, limit=None,
                        min_sqi=_qint(min_sqi), max_price=_qint(max_price),
                        min_dr=_qint(min_dr), min_traffic=_qint(min_traffic))
    fname, buf, media = D.build_export(rows, fmt=("xlsx" if format == "xlsx" else "csv"))
    return StreamingResponse(buf, media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.post("/ui/donors/run")
def ui_donors_run(length: int = Form(100), max_records: int = Form(0)):
    from app.credentials import get_cred
    from app.services import donors as D

    def back(m: str):
        return RedirectResponse(url=f"{BP}/donors?msg={quote(m)}", status_code=303)

    cookie = get_cred("miralinks_cookie")
    body = get_cred("miralinks_body")
    if not cookie or not body:
        return back("Сначала сохраните cookie и тело запроса Miralinks в Настройках.")
    if D.current_status().get("running"):
        return back("Сбор уже идёт — обновите страницу позже.")
    started = D.launch_run(cookie, body, length=max(1, min(length, 500)),
                           max_records=max(0, max_records))
    return back("Запущен сбор каталога Miralinks. Обновляйте страницу — площадки появятся ниже."
                if started else "Сбор уже идёт.")


@router.get("/cannibalization")
def cannibalization_page(request: Request, domain: str | None = None,
                         period_days: int = 90, min_query_impr: str | None = None,
                         min_page_impr: str | None = None, max_position: str | None = None,
                         exclude_home: int = 1, cap: str | None = None,
                         se: list[int] | None = Query(None), db: Session = Depends(get_db)):
    from datetime import timedelta

    from app.services import cannibalization as C
    from app.services import serp as S

    domains = _domains(db)
    cur = domain if domain and any(d["domain"] == domain for d in domains) \
        else (domains[0]["domain"] if domains else None)
    gsc_ids = (_domain_engine_ids(db, cur, ("gsc",)).get("gsc", []) if cur
               else _keyword_site_ids(db, "", ("gsc",)))
    end = date.today() - timedelta(days=1)
    dr = parse_date_range((end - timedelta(days=max(1, period_days) - 1)).isoformat(),
                          end.isoformat())
    mqi = _qint(min_query_impr); mpi = _qint(min_page_impr); mpos = _qint(max_position)
    home = bool(exclude_home)
    show = 500  # rows rendered on the page; export is unlimited
    gsc_all = C.gsc_cannibalization(
        db, gsc_ids, dr, min_query_impr=(mqi if mqi is not None else 30),
        min_page_impr=(mpi if mpi is not None else 10),
        max_position=(float(mpos) if mpos else None), exclude_home=home, limit=None)

    caps = S.captures(db)
    cap_sel = cap or (caps[0] if caps else None)
    own = {cur} if cur else {d["domain"] for d in _domains(db)}
    serp_all = C.serp_cannibalization(db, cap_sel, own, se=(list(se) if se else None),
                                      exclude_home=home, limit=None)
    return templates.TemplateResponse(request, "cannibalization.html", {
        "request": request, "domains": domains, "cur_domain": cur or "",
        "period_days": period_days, "min_query_impr": mqi if mqi is not None else 30,
        "min_page_impr": mpi if mpi is not None else 10, "max_position": mpos or "",
        "exclude_home": home, "gsc_rows": gsc_all[:show], "serp_rows": serp_all[:show],
        "gsc_total": len(gsc_all), "serp_total": len(serp_all), "shown": show,
        "has_gsc": bool(gsc_ids), "captures": caps, "cap": cap_sel,
        "se_sel": [int(x) for x in se] if se else [],
    })


@router.get("/cannibalization/export")
def cannibalization_export(domain: str | None = None, period_days: int = 90,
                           min_query_impr: str | None = None, min_page_impr: str | None = None,
                           max_position: str | None = None, exclude_home: int = 1,
                           format: str = "csv", db: Session = Depends(get_db)):
    from datetime import timedelta

    from app.services import cannibalization as C

    gsc_ids = (_domain_engine_ids(db, domain, ("gsc",)).get("gsc", []) if domain
               else _keyword_site_ids(db, "", ("gsc",)))
    end = date.today() - timedelta(days=1)
    dr = parse_date_range((end - timedelta(days=max(1, period_days) - 1)).isoformat(),
                          end.isoformat())
    mqi = _qint(min_query_impr); mpi = _qint(min_page_impr); mpos = _qint(max_position)
    rows = C.gsc_cannibalization(
        db, gsc_ids, dr, min_query_impr=(mqi if mqi is not None else 30),
        min_page_impr=(mpi if mpi is not None else 10),
        max_position=(float(mpos) if mpos else None), exclude_home=bool(exclude_home),
        limit=None)
    fname, buf, media = C.build_export(rows, domain or "all", fmt=("xlsx" if format == "xlsx" else "csv"))
    return StreamingResponse(buf, media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


def _pages_inputs(db, domain, engine, start, end, b_start, b_end):
    """Resolve domain/engine -> site_ids (one engine, to avoid double-count) and
    periods A (current) and B (previous)."""
    domains = _domains(db)
    cur = domain if domain and any(d["domain"] == domain for d in domains) \
        else (domains[0]["domain"] if domains else None)
    avail = next((d["engines"] for d in domains if d["domain"] == cur), [])
    eng = engine if engine in avail else (avail[0] if avail else "gsc")
    ids = (_domain_engine_ids(db, cur, (eng,)).get(eng, []) if cur
           else _keyword_site_ids(db, "", (eng,)))
    dr_a = parse_date_range(start, end)
    dr_b = resolve_period_b(dr_a, b_start, b_end)
    return domains, cur, avail, eng, ids, dr_a, dr_b


def _pages_filter(db, site_ids):
    """Active 'my pages' list (saved in settings) -> (text, urls, page_ids).

    ``page_ids`` is None when no list is set (show all pages); an explicit list
    (possibly empty) when a list is set — empty means none of the URLs matched
    our data for this source."""
    from app.credentials import get_cred
    from app.services.loaders import resolve_page_ids

    text = get_cred("pages_url_filter") or ""
    urls = _parse_phrases(text)
    if not urls:
        return "", [], None
    return text, urls, resolve_page_ids(db, site_ids, urls)


@router.post("/ui/pages/urls")
async def ui_pages_urls(urls: str = Form(""), clear: int = Form(0),
                        file: UploadFile | None = File(None)):
    from app.credentials import set_cred

    words = [] if clear else _parse_phrases(urls)
    if not clear and file is not None and getattr(file, "filename", ""):
        try:
            words += _parse_phrases((await file.read()).decode("utf-8", "ignore"))
        finally:
            await file.close()
    seen, uniq = set(), []
    for w in words:
        if w.lower() not in seen:
            seen.add(w.lower())
            uniq.append(w)
    set_cred("pages_url_filter", "\n".join(uniq))
    msg = "Список страниц очищен." if not uniq else f"Список страниц сохранён: {len(uniq)} URL."
    return RedirectResponse(url=f"{BP}/pages?msg={quote(msg)}", status_code=303)


@router.get("/pages")
def pages_page(request: Request, domain: str | None = None, engine: str = "gsc",
               start: str | None = None, end: str | None = None, b_start: str | None = None,
               b_end: str | None = None, q: str | None = None, min_impr: str | None = None,
               sort: str = "clicks", msg: str | None = None, db: Session = Depends(get_db)):
    from app.services import page_devices as PD

    domains, cur, avail, eng, ids, dr_a, dr_b = _pages_inputs(
        db, domain, engine, start, end, b_start, b_end)
    url_text, url_list, page_ids = _pages_filter(db, ids)
    show = 500
    res = PD.page_device_compare(db, ids, dr_a, dr_b, page_ids=page_ids, search=(q or None),
                                 min_impressions=_qint(min_impr) or 0, sort=sort, limit=show)
    return templates.TemplateResponse(request, "pages.html", {
        "request": request, "domains": domains, "cur_domain": cur or "", "msg": msg,
        "engines_avail": avail, "engine": eng, "range_a": dr_a, "range_b": dr_b,
        "rows": res["rows"], "total": res["total"], "shown": show, "devices": res["devices"],
        "q": q or "", "min_impr": _qint(min_impr) or "", "sort": sort,
        "url_filter": url_text, "url_count": len(url_list),
        "matched": (len(set(page_ids)) if page_ids is not None else None),
        "has_data": bool(ids),
    })


@router.get("/pages/export")
def pages_export(domain: str | None = None, engine: str = "gsc", start: str | None = None,
                 end: str | None = None, b_start: str | None = None, b_end: str | None = None,
                 q: str | None = None, min_impr: str | None = None, sort: str = "clicks",
                 format: str = "csv", db: Session = Depends(get_db)):
    from app.services import page_devices as PD

    _domains_, cur, _avail, _eng, ids, dr_a, dr_b = _pages_inputs(
        db, domain, engine, start, end, b_start, b_end)
    _t, _u, page_ids = _pages_filter(db, ids)
    res = PD.page_device_compare(db, ids, dr_a, dr_b, page_ids=page_ids, search=(q or None),
                                 min_impressions=_qint(min_impr) or 0, sort=sort, limit=None)
    fname, buf, media = PD.build_export(res["rows"], res["devices"], cur or "all",
                                        fmt=("xlsx" if format == "xlsx" else "csv"))
    return StreamingResponse(buf, media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.post("/ui/backfill")
def ui_backfill(site_id: int = Form(...), days: int = Form(90), db: Session = Depends(get_db)):
    from app.scheduler.jobs import run_backfill

    try:
        run_backfill(db, site_id, days)
    except Exception:  # noqa: BLE001 - surfaced via the runs table on /admin
        pass
    return RedirectResponse(url=f"{BP}/admin", status_code=303)


@router.post("/ui/yandex/connect")
def ui_yandex_connect(token: str = Form(...), backfill_days: int = Form(480),
                      db: Session = Depends(get_db)):
    from app.services.connect import connect_yandex

    try:
        result = connect_yandex(db, token, backfill_days, True)
        msg = (
            f"Яндекс подключён. Сайтов: {len(result['site_ids'])}. "
            "Данные загружаются в фоне — обновите дашборд через 1–2 минуты."
        )
        return RedirectResponse(url=f"{BP}/?msg={quote(msg)}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"{BP}/admin?msg={quote('Ошибка Яндекс: ' + str(exc))}", status_code=303)


@router.post("/ui/gsc/connect")
def ui_gsc_connect(mode: str = Form("oauth"), gsc_json: str = Form(""),
                   client_id: str = Form(""), client_secret: str = Form(""),
                   refresh_token: str = Form(""), backfill_days: int = Form(480),
                   db: Session = Depends(get_db)):
    from app.services.connect import connect_gsc_oauth, connect_gsc_service_account

    try:
        if mode == "service_account":
            result = connect_gsc_service_account(db, gsc_json, backfill_days, True)
        else:
            result = connect_gsc_oauth(db, client_id, client_secret, refresh_token, backfill_days, True)
        n = len(result["site_ids"])
        msg = (
            f"Подключено. Сайтов найдено: {n}. Данные загружаются в фоне — "
            "обновите дашборд через 1–2 минуты."
        )
        return RedirectResponse(url=f"{BP}/?msg={quote(msg)}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"{BP}/admin?msg={quote('Ошибка: ' + str(exc))}", status_code=303)


@router.post("/ui/gsc/oauth/start")
def ui_gsc_oauth_start(request: Request, client_id: str = Form(...),
                       client_secret: str = Form(...), backfill_days: int = Form(480)):
    from app.credentials import set_cred
    from app.services.connect import google_auth_url

    client_id = client_id.strip()
    set_cred("gsc_oauth_client_id", client_id)
    set_cred("gsc_oauth_client_secret", client_secret.strip())
    set_cred("gsc_oauth_backfill_days", str(backfill_days))
    state = secrets.token_urlsafe(16)
    set_cred("gsc_oauth_state", state)
    url = google_auth_url(client_id, _public_redirect_uri(request), state)
    return RedirectResponse(url=url, status_code=303)


@router.get("/oauth/callback", name="gsc_oauth_callback")
def gsc_oauth_callback(request: Request, code: str = "", state: str = "", error: str = "",
                       db: Session = Depends(get_db)):
    from app.credentials import get_cred
    from app.services.connect import connect_gsc_oauth, exchange_code_for_refresh_token

    if error:
        return RedirectResponse(url=f"{BP}/admin?msg={quote('Google: ' + error)}", status_code=303)
    if not code or not state or state != get_cred("gsc_oauth_state"):
        return RedirectResponse(
            url=f"{BP}/admin?msg={quote('Авторизация не подтверждена (state).')}", status_code=303
        )
    try:
        client_id = get_cred("gsc_oauth_client_id")
        client_secret = get_cred("gsc_oauth_client_secret")
        refresh_token = exchange_code_for_refresh_token(
            client_id, client_secret, code, _public_redirect_uri(request)
        )
        days = int(get_cred("gsc_oauth_backfill_days", "480") or 480)
        result = connect_gsc_oauth(db, client_id, client_secret, refresh_token, days, True)
        msg = (
            f"Google подключён. Сайтов: {len(result['site_ids'])}. "
            "Данные загружаются в фоне — обновите дашборд через 1–2 минуты."
        )
        return RedirectResponse(url=f"{BP}/?msg={quote(msg)}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"{BP}/admin?msg={quote('Ошибка: ' + str(exc))}", status_code=303)


@router.get("/projects/{project_id}")
def project_page(request: Request, project_id: int, start: str | None = None,
                 end: str | None = None, order_by: str = "clicks", gran: str = "day",
                 brand: list[str] | None = Query(None),
                 engines: list[str] | None = Query(None),
                 msg: str | None = None, db: Session = Depends(get_db)):
    from app.services import brands as brands_svc
    from app.services.goals import page_key

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    dr = parse_date_range(start, end)
    site = db.get(Site, project.site_id)
    domain = domain_of(site.property_uri) if site else ""

    # Search engines that have this domain + the selected subset (default: all of
    # them). Within an engine its same-host properties (sc-domain: + https://) are
    # de-duped; clicks/impressions are SUMMED across the chosen engines.
    engine_ids_all = _domain_engine_ids(db, domain, SEARCH_ENGINES) if domain else {}
    engines_avail = [e for e in SEARCH_ENGINES if e in engine_ids_all]
    sel = [e for e in (engines or engines_avail) if e in engines_avail] or engines_avail
    parts_ids = [engine_ids_all[e] for e in sel]
    flat_ids = [sid for ids in parts_ids for sid in ids] or ([site.id] if site else None)

    # Brand filter: restrict the project's URLs to the chosen brands of its domain
    brand_list = brands_svc.list_brands(db, domain) if domain else []
    sel_brands = [b for b in (brand or []) if any(x["brand"] == b for x in brand_list)]
    only_norms, brand_keys = None, None
    if sel_brands:
        brand_keys = brands_svc.brand_url_keys(db, domain, sel_brands)
        only_norms = {u.normalized_url for u in project.urls if page_key(u.url) in brand_keys}

    if parts_ids:  # per engine (de-dup within), then summed across engines
        subset = totals_svc.combine_totals(
            [totals_svc.subset_totals(db, project, dr, site_ids=ids, only_norms=only_norms)
             for ids in parts_ids])
        daily = totals_svc.combine_daily(
            [totals_svc.subset_daily(db, project, dr, site_ids=ids, only_norms=only_norms)
             for ids in parts_ids])
    else:
        subset = totals_svc.subset_totals(db, project, dr, only_norms=only_norms)
        daily = totals_svc.subset_daily(db, project, dr, only_norms=only_norms)

    return templates.TemplateResponse(
        request,
        "project_detail.html",
        {
            "request": request,
            "project": project,
            "site": site,
            "range": dr, "msg": msg,
            "order_by": order_by, "gran": gran,
            "engines": sel, "engines_avail": engines_avail,
            "brands": brand_list, "sel_brands": sel_brands,
            "top_keywords": top_keywords_for_project(db, project, dr, order_by,
                                                     site_ids=flat_ids, only_norms=only_norms),
            "subset_totals": subset,
            "daily": totals_svc.bucket_series(daily, gran),
            "goals": _project_goals(db, project, site, dr, gran, brand_keys=brand_keys),
            "brand_goals": _brand_goals(db, project, site, dr, domain, parts_ids=parts_ids),
        },
    )


@router.post("/ui/projects/{project_id}/brands")
async def ui_project_brands(project_id: int, file: UploadFile = File(...),
                            db: Session = Depends(get_db)):
    from app.services import brands as brands_svc

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    try:
        res = brands_svc.import_brand_csv(db, await file.read())
        site = db.get(Site, project.site_id)
        dom = domain_of(site.property_uri) if site else ""
        added = brands_svc.sync_project_urls(db, project, dom)
        msg = (f"Загружено брендов: {res['brands']}, URL: {res['rows']}. "
               f"В проект добавлено URL: {added}.")
    except Exception as exc:  # noqa: BLE001
        msg = f"Ошибка: {exc}"
    finally:
        await file.close()
    return RedirectResponse(url=f"{BP}/projects/{project_id}?msg={quote(msg)}", status_code=303)


def _goals_chart(stats: dict, names: dict, dr, gran: str, top: int = 8) -> dict | None:
    """Time series (at ``gran``) of favourite-goal completions across the
    project's URLs: a line per top goal + 'Прочие' for the rest."""
    from datetime import timedelta

    daily = stats.get("daily") or {}
    if not daily:
        return None
    span = (dr.end - dr.start).days

    def buckets(daymap):
        series = [{"date": (dr.start + timedelta(days=i)).isoformat(),
                   "count": daymap.get((dr.start + timedelta(days=i)).isoformat(), 0)}
                  for i in range(span + 1)]
        return totals_svc.bucket_series(series, gran)

    ranked = [g["id"] for g in stats["by_goal"]]
    labels, series = None, []
    for gid in ranked[:top]:
        b = buckets(daily.get(gid, {}))
        labels = [x["date"] for x in b]
        series.append({"name": names.get(gid, f"Цель {gid}"), "data": [x["count"] for x in b]})
    rest = ranked[top:]
    if rest:
        merged: dict[str, int] = {}
        for gid in rest:
            for d2, c in daily.get(gid, {}).items():
                merged[d2] = merged.get(d2, 0) + c
        b = buckets(merged)
        labels = labels or [x["date"] for x in b]
        series.append({"name": "Прочие", "data": [x["count"] for x in b]})
    return {"labels": labels or [], "series": series} if series else None


def _project_goals(db, project, site, dr, gran, brand_keys=None):
    """Metrica goal completions on the project's entrance pages (favourites),
    optionally restricted to the URLs of the selected brands."""
    from app.services import goals as goals_svc

    if site is None:
        return None
    vids, _ = _same_domain_site_ids(db, site)  # sites of this domain holding visits
    url_for_key = {}
    for (u,) in db.execute(
        select(ProjectUrl.url).where(ProjectUrl.project_id == project.id)
    ).all():
        k = goals_svc.page_key(u)
        if brand_keys is None or k in brand_keys:
            url_for_key.setdefault(k, u)
    if not url_for_key:
        return None
    favs = goals_svc.parse_favorites(project.favorite_goals)
    stats = goals_svc.goal_stats(db, vids, dr, url_for_key, favorites=(favs or None))
    names = goals_svc.goal_names(db, vids)
    return {"stats": stats, "names": names, "favorites": favs,
            "chart": _goals_chart(stats, names, dr, gran)}


def _brand_goals(db, project, site, dr, domain, parts_ids=None):
    """Search clicks AND favourite-goal completions grouped by brand (the brand of
    the project/landing page), across ALL brands of the domain. ``None`` if the
    project has no brand map. A URL tied to several brands counts under each.
    ``parts_ids`` = per-engine site-id lists (clicks de-duped within an engine,
    summed across engines); None = the project's own site."""
    from app.db.models import UrlBrand
    from app.services import goals as goals_svc

    if site is None or not domain:
        return None
    brand_rows = db.execute(
        select(UrlBrand.url_key, UrlBrand.brand).where(UrlBrand.domain == domain)
    ).all()
    if not brand_rows:
        return None
    vids, _ = _same_domain_site_ids(db, site)
    url_for_key = {}
    for (u,) in db.execute(
        select(ProjectUrl.url).where(ProjectUrl.project_id == project.id)
    ).all():
        url_for_key.setdefault(goals_svc.page_key(u), u)
    if not url_for_key:
        return None
    favs = goals_svc.parse_favorites(project.favorite_goals)
    stats = goals_svc.goal_stats(db, vids, dr, url_for_key, favorites=(favs or None), top=10**9)
    key_goals = {goals_svc.page_key(r["url"]): r["count"] for r in stats["by_url"]}
    key_clicks: dict[str, int] = {}
    for ids in (parts_ids or [None]):          # per engine: de-dup within, sum across
        for p in ctr_for_project(db, project, dr, site_ids=ids, only_norms=None)["pages"]:
            k = goals_svc.page_key(p["url"])
            key_clicks[k] = key_clicks.get(k, 0) + int(p["clicks"] or 0)
    agg: dict[str, dict] = {}
    for url_key, brand in brand_rows:
        a = agg.setdefault(brand, {"goals": 0, "clicks": 0})
        a["goals"] += key_goals.get(url_key, 0)
        a["clicks"] += key_clicks.get(url_key, 0)
    rows = sorted(({"brand": b, "goals": v["goals"], "clicks": v["clicks"],
                    "conv": round(v["goals"] / v["clicks"] * 100, 1) if v["clicks"] else None}
                   for b, v in agg.items()),
                  key=lambda r: (-r["goals"], -r["clicks"], r["brand"]))
    return {"rows": rows, "total": stats["total"],
            "total_clicks": sum(key_clicks.values()), "favorites": favs}


@router.post("/ui/projects/{project_id}/goals")
def ui_project_goals(project_id: int, goal: list[int] = Form(default=[]),
                     db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    project.favorite_goals = ",".join(str(g) for g in goal) if goal else None
    db.commit()
    return RedirectResponse(url=f"{BP}/projects/{project_id}", status_code=303)


@router.get("/projects/{project_id}/compare")
def project_compare_page(request: Request, project_id: int, metric: str = "clicks",
                         a_start: str | None = None, a_end: str | None = None,
                         b_start: str | None = None, b_end: str | None = None,
                         clean: int = 0, ratio: float = 10.0, min_impr: int = 100,
                         device: str = "all", merge: int = 0, db: Session = Depends(get_db)):
    from app.services.multi_compare import ENGINE_LABELS, ENGINES, compare_project

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    period_a = parse_date_range(a_start, a_end)
    period_b = resolve_period_b(period_a, b_start, b_end)
    result = compare_project(db, project, metric, period_a, period_b,
                             exclude_bots=bool(clean), ratio=ratio, min_impr=min_impr,
                             device=device, merge=bool(merge))
    return templates.TemplateResponse(
        request,
        "project_compare.html",
        {
            "request": request,
            "project": project,
            "metric": result["metric"],
            "clean": bool(clean), "ratio": ratio, "min_impr": min_impr,
            "device": result["device"], "merge": bool(merge),
            "period_a": period_a,
            "period_b": period_b,
            "result": result,
            "engine_list": [(code, ENGINE_LABELS[code]) for code in ENGINES],
        },
    )


@router.get("/compare")
def compare_page(request: Request, site_id: int | None = None, metric: str = "clicks",
                 grouping: str = "site", project_id: int | None = None,
                 a_start: str | None = None, a_end: str | None = None,
                 b_start: str | None = None, b_end: str | None = None,
                 min_impressions: int = 0, clean: int = 0, ratio: float = 10.0,
                 min_impr: int = 100, merge: int = 0, db: Session = Depends(get_db)):
    sites = _sites(db)
    site = _resolve_site(db, site_id)
    result = None
    merged = None
    period_a = parse_date_range(a_start, a_end)
    period_b = resolve_period_b(period_a, b_start, b_end)
    if site is not None and (a_start or b_start or site_id):
        ids = site.id
        if merge:
            mids = _same_domain_ids(db, site)
            if len(mids) > 1:
                ids = mids
                merged = {"count": len(mids), "domain": domain_of(site.property_uri)}
        page_ids = None
        if grouping in ("subset", "page", "query") and project_id:
            project = db.get(Project, project_id)
            if project is not None:
                page_ids = project_page_ids(db, ids, project)
        result = growth.compare(
            db, ids, metric, period_a=period_a, period_b=period_b,
            grouping=grouping, page_ids=page_ids, min_impressions=min_impressions,
            exclude_bots=bool(clean), ratio=ratio, min_impr=min_impr,
        )
    return templates.TemplateResponse(
        request,
        "compare.html",
        {
            "request": request,
            "sites": sites,
            "site": site,
            "projects": db.execute(select(Project).order_by(Project.id)).scalars().all(),
            "metric": metric,
            "grouping": grouping,
            "project_id": project_id,
            "min_impressions": min_impressions,
            "clean": bool(clean), "ratio": ratio, "min_impr": min_impr,
            "merge": bool(merge), "merged": merged,
            "period_a": period_a,
            "period_b": period_b,
            "result": result,
        },
    )


def _indexing_ctx(db, request, site_id, a=None, b=None, msg=None, check=None, gran="day",
                  start=None, end=None):
    from app.services import google_index as gi
    from app.services import indexing
    from app.services import indexnow as inx

    sites = _sites(db)
    site = _resolve_site(db, site_id)
    snapshots, history, cmp, can_capture = [], [], None, False
    g_inspect = g_submit = None
    g_can_inspect = g_can_submit = False
    if site is not None:
        can_capture = indexing.supports(site)
        snapshots = indexing.list_snapshots(db, site.id)
        # index size is a STOCK, not a flow — roll up by last snapshot per bucket
        hist = indexing.count_history(db, site)
        if start or end:  # optional period filter for the chart
            lo, hi = start or "0000-01-01", end or "9999-12-31"
            hist = [h for h in hist if lo <= (h.get("date") or "") <= hi]
        history = totals_svc.bucket_series(hist, gran, agg="last")
        dates = [s["date"] for s in snapshots]
        da = a or (dates[0] if dates else None)
        db_ = b or (dates[1] if len(dates) > 1 else None)
        if da and db_:
            cmp = indexing.compare_snapshots(db, site.id, date.fromisoformat(da), date.fromisoformat(db_))
            a, b = da, db_
        g_can_inspect = gi.supports_inspection(site)
        g_can_submit = gi.supports_submit(site)
        g_inspect = gi.load_result("inspect", site.id)
        g_submit = gi.load_result("submit", site.id)
    g_running = bool((g_inspect and g_inspect.get("running")) or (g_submit and g_submit.get("running")))
    indexnow_info = inx.info(db, site) if site is not None else None
    return {
        "request": request, "msg": msg, "sites": sites, "site": site,
        "snapshots": snapshots, "history": history, "cmp": cmp,
        "a": a, "b": b, "can_capture": can_capture, "check": check, "gran": gran,
        "h_start": start, "h_end": end,
        "g_inspect": g_inspect, "g_submit": g_submit, "g_running": g_running,
        "g_can_inspect": g_can_inspect, "g_can_submit": g_can_submit,
        "indexnow": indexnow_info,
    }


@router.get("/indexing")
def indexing_page(request: Request, site_id: int | None = None, a: str | None = None,
                  b: str | None = None, msg: str | None = None, gran: str = "day",
                  start: str | None = None, end: str | None = None,
                  db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "indexing.html",
        _indexing_ctx(db, request, site_id, a, b, msg, gran=gran, start=start, end=end),
    )


@router.post("/ui/indexing/check")
async def ui_indexing_check(request: Request, site_id: int = Form(...),
                            urls_text: str | None = Form(None),
                            file: UploadFile | None = File(None),
                            db: Session = Depends(get_db)):
    from app.services import indexing

    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, "site not found")
    raw = ""
    if file is not None:
        raw += (await file.read()).decode("utf-8", errors="ignore") + "\n"
    if urls_text:
        raw += urls_text
    urls = [line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()]
    check = indexing.check_urls(db, site, urls)
    if check:
        check["input_text"] = "\n".join(r["input"] for r in check["rows"])
    msg = None if check else "Сначала создайте снимок страниц в индексе (кнопка выше) — потом проверяйте список."
    return templates.TemplateResponse(
        request, "indexing.html", _indexing_ctx(db, request, site_id, msg=msg, check=check)
    )


@router.post("/ui/indexing/check/export")
async def ui_indexing_check_export(site_id: int = Form(...), urls_text: str | None = Form(None),
                                   export: str = Form("out:txt"), db: Session = Depends(get_db)):
    """Re-run the check and return matching URLs as a download. `export` = "<only>:<fmt>"
    where only ∈ {out,in,all} and fmt ∈ {txt,csv}."""
    import csv
    import io

    from app.services import indexing

    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, "site not found")
    only, _, fmt = export.partition(":")
    urls = [line.strip() for line in (urls_text or "").replace(",", "\n").splitlines() if line.strip()]
    check = indexing.check_urls(db, site, urls)
    if not check:
        return RedirectResponse(
            url=f"{BP}/indexing?site_id={site_id}&msg={quote('Нет снимка для проверки — создайте снимок.')}",
            status_code=303,
        )
    if only == "in":
        rows = [r for r in check["rows"] if r["in_index"]]
    elif only == "all":
        rows = check["rows"]
    else:
        only, rows = "out", [r for r in check["rows"] if not r["in_index"]]
    tag = {"in": "in_index", "all": "index_check", "out": "not_indexed"}[only]

    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["url", "in_index", "title"])
        for r in rows:
            w.writerow([r["input"], int(r["in_index"]), r["title"] or ""])
        # BOM, иначе Excel на русской Windows читает UTF-8 как cp1251 («кракозябры»)
        body, media, ext = "\ufeff" + buf.getvalue(), "text/csv; charset=utf-8", "csv"
    else:
        body = "".join(r["input"] + "\n" for r in rows)
        body, media, ext = body, "text/plain; charset=utf-8", "txt"

    fname = f"{tag}_site{site_id}_{check['captured_on']}.{ext}"
    return Response(content=body, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.post("/ui/indexing/capture")
def ui_indexing_capture(site_id: int = Form(...), db: Session = Depends(get_db)):
    import threading

    from app.services import indexing

    site = db.get(Site, site_id)
    if site is None or not indexing.supports(site):
        return RedirectResponse(
            url=f"{BP}/indexing?site_id={site_id}&msg={quote('Этот источник не отдаёт список страниц в индексе (доступно для Яндекса).')}",
            status_code=303,
        )
    threading.Thread(target=indexing.capture_async, args=(site_id,), daemon=True).start()
    return RedirectResponse(
        url=f"{BP}/indexing?site_id={site_id}&msg={quote('Снимок страниц в индексе создаётся в фоне — обновите через минуту.')}",
        status_code=303,
    )


@router.get("/ui/cache/flush")
def ui_cache_flush(request: Request):
    """Drop every cached page, then return to where you were."""
    from app.cache import flush

    n = flush()
    ref = request.headers.get("referer")
    base = ref.split("?")[0] if ref else f"{BP}/"
    return RedirectResponse(url=f"{base}?msg={quote(f'Кэш страниц сброшен ({n} шт.).')}", status_code=303)


async def _read_url_list(urls_text: str | None, file: UploadFile | None) -> list[str]:
    """URLs from a textarea and/or an uploaded .txt/.csv (comma or newline separated)."""
    raw = ""
    if file is not None:
        raw += (await file.read()).decode("utf-8", errors="ignore") + "\n"
    if urls_text:
        raw += urls_text
    return [line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()]


def _index_redirect(site_id: int, msg: str) -> RedirectResponse:
    return RedirectResponse(url=f"{BP}/indexing?site_id={site_id}&msg={quote(msg)}", status_code=303)


@router.post("/ui/indexing/google/check")
async def ui_google_check(site_id: int = Form(...), urls_text: str | None = Form(None),
                          file: UploadFile | None = File(None), db: Session = Depends(get_db)):
    from app.services import google_index as gi

    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, "site not found")
    if not gi.supports_inspection(site):
        return _index_redirect(site_id, "Проверка индексации постранично доступна только для сайтов Google Search Console.")
    urls = await _read_url_list(urls_text, file)
    if not urls:
        return _index_redirect(site_id, "Добавьте список URL для проверки.")
    _, msg = gi.start_inspect(site_id, urls)
    return _index_redirect(site_id, msg)


@router.post("/ui/indexing/google/submit")
async def ui_google_submit(site_id: int = Form(...), urls_text: str | None = Form(None),
                           file: UploadFile | None = File(None), confirm: str | None = Form(None),
                           db: Session = Depends(get_db)):
    from app.services import google_index as gi

    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, "site not found")
    if not gi.supports_submit(site):
        return _index_redirect(site_id, "Отправка на индексацию доступна только для сайтов Google Search Console.")
    if confirm != "yes":  # first of the two warnings (the second is a JS confirm dialog)
        return _index_redirect(site_id, "Отправка не подтверждена — поставьте галочку подтверждения.")
    urls = await _read_url_list(urls_text, file)
    if not urls:
        return _index_redirect(site_id, "Добавьте список URL для отправки.")
    _, msg = gi.start_submit(site_id, urls, "URL_UPDATED")
    return _index_redirect(site_id, msg)


@router.post("/ui/indexing/google/check/export")
def ui_google_check_export(site_id: int = Form(...), export: str = Form("out:txt"),
                           db: Session = Depends(get_db)):
    """Export the stored inspection result. `export` = "<only>:<fmt>",
    only ∈ {out,in,all}, fmt ∈ {txt,csv}."""
    import csv
    import io

    from app.services import google_index as gi

    res = gi.load_result("inspect", site_id)
    if not res or not res.get("rows"):
        return _index_redirect(site_id, "Нет результатов проверки для выгрузки.")
    only, _, fmt = export.partition(":")
    rows = res["rows"]
    if only == "in":
        rows = [r for r in rows if r.get("indexed")]
    elif only == "all":
        pass
    else:
        only, rows = "out", [r for r in rows if not r.get("indexed")]
    tag = {"in": "indexed", "all": "index_check", "out": "not_indexed"}[only]

    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["url", "indexed", "verdict", "coverage", "last_crawl", "canonical", "error"])
        for r in rows:
            w.writerow([r["input"], "" if r.get("indexed") is None else int(bool(r.get("indexed"))),
                        r.get("verdict") or "", r.get("coverage") or "",
                        r.get("last_crawl") or "", r.get("canonical") or "", r.get("error") or ""])
        # BOM, иначе Excel на русской Windows читает UTF-8 как cp1251 («кракозябры»)
        body, media, ext = "\ufeff" + buf.getvalue(), "text/csv; charset=utf-8", "csv"
    else:
        body, media, ext = "".join(r["input"] + "\n" for r in rows), "text/plain; charset=utf-8", "txt"

    fname = f"google_{tag}_site{site_id}.{ext}"
    return Response(content=body, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.post("/ui/indexing/indexnow/submit")
async def ui_indexnow_submit(site_id: int = Form(...), urls_text: str | None = Form(None),
                             file: UploadFile | None = File(None), db: Session = Depends(get_db)):
    """Push the URL list to IndexNow (Bing/Yandex) — one request, no OAuth."""
    from app.services import indexnow

    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, "site not found")
    urls = await _read_url_list(urls_text, file)
    if not urls:
        return _index_redirect(site_id, "Добавьте список URL для отправки в IndexNow.")
    res = indexnow.submit(db, site, urls)
    if res.get("results"):
        parts = "; ".join(f"{r['engine']} — {r['message']}" for r in res["results"])
        msg = f"IndexNow {res['host']} ({res['count']} URL): {parts}"
    else:
        msg = f"IndexNow ({res['host']}): {res.get('message', '')}"
    return _index_redirect(site_id, msg)


@router.get("/ui/indexing/indexnow/keyfile")
def ui_indexnow_keyfile(db: Session = Depends(get_db)):
    """Download the IndexNow key file to upload to the site root as <key>.txt."""
    from app.services import indexnow

    key = indexnow.get_key(db)
    return Response(content=key, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{key}.txt"'})


def _same_domain_site_ids(db: Session, site: Site) -> tuple[list[int], str]:
    """All site_ids of the same bare domain (any source) + that domain. Metrica
    rows live under whichever twin holds them, so 404/visit queries span them."""
    d = domain_of(site.property_uri)
    ids = [sid for sid, uri in db.execute(select(Site.id, Site.property_uri)).all()
           if d and domain_of(uri) == d] or [site.id]
    return ids, d


@router.get("/errors")
def errors_page(request: Request, site_id: str | None = None, start: str | None = None,
                end: str | None = None, markers: str | None = None, gran: str = "day",
                db: Session = Depends(get_db)):
    from app.services import not_found

    sites = _sites(db)
    dr = parse_date_range(start, end)
    sid = int(site_id) if site_id else None  # "" / None -> overview of all domains
    site = db.get(Site, sid) if sid else None
    ctx = {"request": request, "sites": sites, "site": site, "range": dr, "gran": gran,
           "stats": None, "overview": None, "domain": None,
           "markers": markers if markers is not None else ", ".join(not_found.DEFAULT_MARKERS)}
    if site is not None:  # detailed view for one domain
        ids, domain = _same_domain_site_ids(db, site)
        ctx["domain"] = domain
        ctx["stats"] = not_found.not_found_stats(db, ids, dr, markers, site_domain=domain, gran=gran)
    else:  # overview: a chart per domain that has any 404, last bucket vs previous
        ctx["overview"] = not_found.not_found_overview(db, dr, markers, gran=gran)
    return templates.TemplateResponse(request, "errors.html", ctx)


@router.get("/webvisor")
def webvisor_page(request: Request, limit: int = 300, db: Session = Depends(get_db)):
    """List recorded Webvisor replays (newest first) with full visit context; play inline."""
    import re as _re

    from app.services import goals as goals_svc

    videos, total = [], 0
    if WEBVISOR_VIDEOS.is_dir():
        files = sorted(WEBVISOR_VIDEOS.glob("*.webm"), key=lambda p: p.stat().st_mtime, reverse=True)
        total = len(files)
        files = files[:max(1, limit)]
        ids = [p.stem for p in files]
        info, phrases = {}, _wv_phrases()
        gnames, hit_url = {}, {}
        if ids:
            from app.db.models import Hit, Visit
            cols = (Visit.visit_id, Visit.date, Visit.date_time, Visit.duration, Visit.page_views,
                    Visit.traffic_source, Visit.search_engine, Visit.region_city, Visit.device,
                    Visit.os, Visit.browser, Visit.start_url, Visit.end_url, Visit.referer,
                    Visit.counter_id, Visit.extra, Visit.watch_ids, Visit.site_id)
            for row in db.execute(select(*cols).where(Visit.visit_id.in_(ids))).all():
                info[str(row[0])] = row
            # goal names for counters whose visits reached goals (best-effort, cached)
            for sid in {row[17] for row in info.values()
                        if row[17] and "goalsID" in (row[15] or "")}:
                try:
                    gnames.update(goals_svc.goal_names(db, [sid]))
                except Exception:  # noqa: BLE001
                    pass
            # pages visited: visit.watch_ids -> hit.url (grouped by site -> uses the index)
            by_site: dict = {}
            for row in info.values():
                if row[17]:
                    by_site.setdefault(row[17], []).extend(_re.findall(r"\d+", row[16] or ""))
            for sid, wids in by_site.items():
                wids = list(dict.fromkeys(wids))
                for i in range(0, len(wids), 800):
                    for wid, url in db.execute(
                        select(Hit.watch_id, Hit.url).where(
                            Hit.site_id == sid, Hit.watch_id.in_(wids[i:i + 800]))
                    ).all():
                        if url:
                            hit_url[str(wid)] = url
        for p in files:
            r = info.get(p.stem)
            extra = r[15] if r else None
            goal_ids = sorted(goals_svc.parse_goal_ids(extra))
            pages, seen = [], set()
            for w in (_re.findall(r"\d+", r[16] or "") if r else []):
                u = hit_url.get(w)
                if u and u not in seen:
                    seen.add(u)
                    pages.append(u)
            videos.append({
                "visit_id": p.stem,
                "size_mb": round(p.stat().st_size / 1048576, 2),
                "date": (r[1].isoformat() if r and r[1] else None),
                "date_time": (r[2] if r else None),
                "duration": (int(r[3]) if r and r[3] else None),
                "page_views": (int(r[4]) if r and r[4] else None),
                "source": (r[5] if r else None),
                "search_engine": (r[6] if r else None),
                "city": (r[7] if r else None),
                "device": (r[8] if r else None),
                "os": (r[9] if r else None),
                "browser": (r[10] if r else None),
                "entry": (r[11] if r else None),
                "exit": (r[12] if r else None),
                "referer": (r[13] if r else None),
                "counter_id": (r[14] if r else None),
                "phrase": phrases.get(p.stem),
                "roistat": _wv_roistat(r[11] if r else None, extra),
                "utm": _wv_utm(extra),
                "goals": [gnames.get(g, f"Цель {g}") for g in goal_ids],
                "pages": pages,
            })
    return templates.TemplateResponse(
        request, "webvisor.html",
        {"request": request, "videos": videos, "total": total, "shown": len(videos)})


@router.get("/webvisor/video/{name}")
def webvisor_video(name: str):
    """Serve a recorded replay file by name (range requests supported for seeking)."""
    if not _WEBM_RE.match(name):
        raise HTTPException(404, "not found")
    path = WEBVISOR_VIDEOS / name
    if not path.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(str(path), media_type="video/webm")


@router.get("/leads")
def leads_page(request: Request, domain: str | None = None, start: str | None = None,
               end: str | None = None, g: list[int] = Query(default=[]), f: int = 0,
               msg: str | None = None, db: Session = Depends(get_db)):
    """Заявки с рекламы: ad visits that reached a goal — entry page, page path,
    goal page. Standalone; pick one domain or all. (f=1 ⇒ use checked goals;
    otherwise the saved favourites for the scope.)"""
    from app.services import leads as L

    dr = parse_date_range(start, end)
    doms = L.domains_with_ads(db, dr)
    cur = domain if (domain and domain in doms) else (domain or "")  # "" = все домены
    site_ids = L.visit_site_ids(db, cur or None)
    available = L.available_goals(db, site_ids, dr)
    selected = set(g) if f else L.get_favorites(db, cur or "all")
    rows = L.leads(db, site_ids, dr, selected or None)
    return templates.TemplateResponse(request, "leads.html", {
        "request": request, "msg": msg, "domains": doms, "cur_domain": cur,
        "range": dr, "available": available, "selected": selected, "rows": rows,
        "has_hits": any(r["path"] for r in rows),
    })


@router.post("/ui/leads/goals")
def ui_leads_goals(domain: str = Form(""), start: str = Form(""), end: str = Form(""),
                   g: list[int] = Form(default=[]), db: Session = Depends(get_db)):
    from app.services import leads as L

    L.set_favorites(db, domain or "all", g)
    qs = f"?domain={quote(domain)}&f=1" + "".join(f"&g={x}" for x in g)
    qs += (f"&start={start}" if start else "") + (f"&end={end}" if end else "")
    return RedirectResponse(url=f"{BP}/leads{qs}&msg={quote('Избранные цели сохранены.')}",
                            status_code=303)


@router.get("/leads/export")
def leads_export(domain: str | None = None, start: str | None = None, end: str | None = None,
                 g: list[int] = Query(default=[]), f: int = 0, db: Session = Depends(get_db)):
    import csv
    import io

    from app.services import leads as L

    dr = parse_date_range(start, end)
    cur = domain or ""
    site_ids = L.visit_site_ids(db, cur or None)
    selected = set(g) if f else L.get_favorites(db, cur or "all")
    rows = L.leads(db, site_ids, dr, selected or None)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["дата", "источник", "utm", "входная страница", "путь",
                "страница заявки", "цели", "город", "устройство"])
    for r in rows:
        w.writerow([
            r["date_time"] or r["date"] or "", r["source"] or "",
            " ".join(f"{k}={v}" for k, v in r["utm"].items()),
            r["entry"] or "", " → ".join(r["path"]), r["goal_page"] or "",
            ", ".join(r["goals"]), r["city"] or "", r["device"] or "",
        ])
    fname = f"leads_{cur or 'all'}_{dr.start}_{dr.end}.csv"
    return Response(content="\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


def _month_to_date(s: str | None) -> date | None:
    """Accept 'YYYY-MM' (month input) or 'YYYY-MM-DD' → date (first of month for 'YYYY-MM')."""
    if not s:
        return None
    s = s.strip()
    try:
        return date.fromisoformat(s if len(s) == 10 else s + "-01")
    except ValueError:
        return None


@router.get("/demand")
def demand_page(request: Request, region: str | None = None, device: str | None = None,
                start: str | None = None, end: str | None = None, search: str | None = None,
                list: str = "on", mode: str = "sep", dedup: int = 0, aud: int = 0,
                gran: str = "month", match: str = "broad", base: str = "прокомпрессор",
                q: list[str] = Query(default=[]),
                msg: str | None = None, db: Session = Depends(get_db)):
    """Спрос: частотность Wordstat по собранным фразам — график + таблица.
    ``base`` — именованная база ключей (вкладки «Спрос прокомпрессор» / «Спрос meyer»);
    ``gran`` — month|week|day; ``match`` — broad|phrase|exact|order. Фильтры: регион,
    устройство, период, поиск, список. ``dedup=1`` — не считать смысловые дубли."""
    from app.services import demand

    base = base if base in demand.BASES else demand.BASES[0]
    gran = gran if gran in ("month", "week", "day") else "month"
    mt = match if match in ("broad", "phrase", "exact", "order") else "broad"
    regs = demand.regions(db, gran, mt)
    devs = demand.devices(db, gran, mt)
    cur_region = region if region in regs else (regs[0] if regs else None)
    cur_device = device if device in devs else (devs[0] if devs else None)
    lo, hi = demand.bounds(db, cur_region, cur_device, gran, mt)
    d_start = _month_to_date(start) or lo
    d_end = _month_to_date(end) or hi
    s = (search or "").strip() or None
    keylist = demand.get_keylist(db, base)
    use_list = list != "off" and bool(keylist)
    list_keyset = {demand.norm_key(k) for k in keylist} if keylist else None
    keyset = list_keyset if use_list else None
    dd = bool(dedup)

    def scope(keyset_=None, srch=None):
        _m, ph = demand.load(db, cur_region, cur_device, d_start, d_end, srch,
                             keyset=keyset_, granularity=gran, match=mt)
        return (_m, demand.dedup_groups(ph) if dd else ph)

    # table scope: what the user browses (filtered by list + search)
    months, phrases = scope(keyset, s)
    raw_count = len(demand.load(db, cur_region, cur_device, d_start, d_end, s,
                                keyset=keyset, granularity=gran, match=mt)[1])
    found = {demand.norm_key(p["query"]) for p in phrases}
    missing = [k for k in keylist if demand.norm_key(k) not in found] if use_list else []
    chosen = set(q)

    # chart scope depends on the selected mode
    is_sum, chart_months, plot = False, months, []
    if mode == "sum_db":  # сумма по всем фразам в базе (без фильтра списка/поиска)
        chart_months, allp = scope()
        agg = demand.aggregate(f"Сумма по всем в базе ({len(allp)} фраз)", allp)
        plot, is_sum = ([agg] if agg else []), True
    elif mode == "sum_list" and keylist:  # сумма по загруженному списку
        chart_months, lp = scope(list_keyset)
        agg = demand.aggregate(f"Сумма по списку ({len(lp)} фраз)", lp)
        plot, is_sum = ([agg] if agg else []), True
    elif mode == "sum_checked":  # сумма отмеченных (или всех показанных, если ничего не отмечено)
        chosen_p = [p for p in phrases if p["query"] in chosen] or phrases
        agg = demand.aggregate(f"Сумма отмеченных ({len(chosen_p)} фраз)", chosen_p)
        plot, is_sum = ([agg] if agg else []), True
    else:  # sep — отмеченные по отдельности (или топ-8 показанных)
        mode = "sep"
        plot = [p for p in phrases if p["query"] in chosen] or phrases[:8]

    # optional: MONTHLY audience estimate (Chapman/IP+UA) across ALL sites, to compare
    # with the (monthly) Wordstat demand. Window = Wordstat period, capped to ~14 months.
    aud_days = aud_est = aud_median = aud_range = None
    if aud:
        from datetime import timedelta

        from app.providers.base import DateRange
        from app.services import audience as A
        aud_sites = [x["id"] for x in A.sites_with_visits(db)]
        if len(aud_sites) >= 2:
            a_end = date.today()
            floor = a_end - timedelta(days=430)  # ~14 месяцев (визиты Метрики — свежие)
            a_start = max(d_start, floor) if d_start else floor
            res = A.estimate(db, aud_sites, DateRange(start=a_start, end=a_end),
                             source="ad_kw_search", mode="ip_ua", bucket="month")
            aud_days = res["days"]
            aud_est, aud_median = res["period_estimate"], res["daily_median"]
            aud_range = (a_start.strftime("%Y-%m"), a_end.strftime("%Y-%m"))

    return templates.TemplateResponse(request, "demand.html", {
        "request": request, "has_data": demand.has_data(db, gran, mt),
        "has_month": demand.has_data(db, "month", mt), "gran": gran, "msg": msg,
        "match": mt, "match_types": demand.match_types(db, gran),
        "base": base, "bases": demand.BASES,
        "regions": regs, "devices": devs, "cur_region": cur_region, "cur_device": cur_device,
        "m_start": d_start.strftime("%Y-%m") if d_start else "",
        "m_end": d_end.strftime("%Y-%m") if d_end else "",
        "search": search or "", "months": chart_months, "phrases": phrases,
        "plot": plot, "chosen": chosen, "mode": mode, "is_sum": is_sum,
        "keylist": keylist, "use_list": use_list, "list_state": list,
        "missing": missing, "dedup": dd, "raw_count": raw_count,
        "aud": bool(aud), "aud_days": aud_days, "aud_est": aud_est,
        "aud_median": aud_median, "aud_range": aud_range,
    })


@router.post("/ui/demand/list")
async def ui_demand_list(urls_text: str | None = Form(None), base: str = Form("прокомпрессор"),
                         file: UploadFile | None = File(None), db: Session = Depends(get_db)):
    """Сохранить базу ключей для вкладки «Спрос <base>» (textarea + файл)."""
    from app.services import demand

    raw = ""
    if file is not None and file.filename:
        raw += (await file.read()).decode("utf-8", errors="ignore") + "\n"
    if urls_text:
        raw += urls_text
    phrases = demand.set_keylist(db, raw, base)
    msg = f"База «{base}» сохранена: {len(phrases)} ключей." if phrases else "База очищена."
    return RedirectResponse(url=f"{BP}/demand?base={quote(base)}&list=on&msg={quote(msg)}",
                            status_code=303)


@router.post("/ui/demand/list/clear")
def ui_demand_list_clear(base: str = Form("прокомпрессор"), db: Session = Depends(get_db)):
    from app.services import demand

    demand.clear_keylist(db, base)
    return RedirectResponse(
        url=f"{BP}/demand?base={quote(base)}&msg={quote('База ключей очищена.')}", status_code=303)


def _demand_redirect(list, region, device, start, end, search, msg, base="прокомпрессор"):
    qs = (f"?base={quote(base)}&list={quote(list or 'on')}&region={quote(region or '')}"
          f"&device={quote(device or '')}&start={start or ''}&end={end or ''}"
          f"&search={quote(search or '')}&msg={quote(msg)}")
    return RedirectResponse(url=f"{BP}/demand{qs}", status_code=303)


@router.post("/ui/demand/delete")
def ui_demand_delete(q: list[str] = Form(default=[]), list: str = Form("on"),
                     base: str = Form("прокомпрессор"),
                     region: str = Form(""), device: str = Form(""), start: str = Form(""),
                     end: str = Form(""), search: str = Form(""), db: Session = Depends(get_db)):
    """Удалить отмеченные фразы целиком: собранные данные Wordstat + запись в базе."""
    from app.services import demand

    n = demand.delete_phrases(db, q, base)
    msg = f"Удалено фраз: {n} (с собранными данными)." if n else "Не выбрано ни одной фразы."
    return _demand_redirect(list, region, device, start, end, search, msg, base)


@router.post("/ui/demand/clear-all")
def ui_demand_clear_all(db: Session = Depends(get_db)):
    """Удалить ВСЮ собранную историю Wordstat и список ключей."""
    from app.services import demand

    n = demand.clear_all(db)
    return RedirectResponse(
        url=f"{BP}/demand?msg={quote(f'Удалено всё: {n} строк истории и список ключей.')}",
        status_code=303)


@router.get("/demand/export")
def demand_export(region: str | None = None, device: str | None = None, start: str | None = None,
                  end: str | None = None, search: str | None = None, list: str = "on",
                  dedup: int = 0, gran: str = "month", match: str = "broad",
                  base: str = "прокомпрессор", db: Session = Depends(get_db)):
    import csv
    import io

    from app.services import demand

    base = base if base in demand.BASES else demand.BASES[0]
    gran = gran if gran in ("month", "week", "day") else "month"
    mt = match if match in ("broad", "phrase", "exact", "order") else "broad"
    regs = demand.regions(db, gran, mt)
    devs = demand.devices(db, gran, mt)
    cur_region = region if region in regs else (regs[0] if regs else None)
    cur_device = device if device in devs else (devs[0] if devs else None)
    lo, hi = demand.bounds(db, cur_region, cur_device, gran, mt)
    d_start = _month_to_date(start) or lo
    d_end = _month_to_date(end) or hi
    keylist = demand.get_keylist(db, base)
    keyset = ({demand.norm_key(k) for k in keylist} if (list != "off" and keylist) else None)
    months, phrases = demand.load(db, cur_region, cur_device, d_start, d_end,
                                  (search or "").strip() or None, keyset=keyset,
                                  granularity=gran, match=mt)
    if dedup:
        phrases = demand.dedup_groups(phrases)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["фраза", *months, "мин", "средн", "макс", "рост,%", "дубли"])
    for p in phrases:
        w.writerow([p["query"], *[("" if v is None else v) for v in p["series"]],
                    p["min"], p["avg"], p["max"], "" if p["change"] is None else p["change"],
                    "; ".join(p.get("dupes", []))])
    fname = f"demand_{cur_region or 'all'}_{cur_device or 'all'}.csv"
    # BOM, иначе Excel на русской Windows читает UTF-8 как cp1251 («кракозябры»)
    return Response(content="\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/audience")
def audience_page(request: Request, sites: list[int] = Query(default=[]),
                  start: str | None = None, end: str | None = None,
                  source: str = "ad_kw_search", kw: str | None = None,
                  mode: str = "ip_ua", db: Session = Depends(get_db)):
    """Аудитория / охват: оценка уникальной аудитории методом повторного отлова
    (capture-recapture по IP+UA между сайтами за день)."""
    from app.services import audience as A

    all_sites = A.sites_with_visits(db)
    ids = [i for i in sites if i in {s["id"] for s in all_sites}] or [s["id"] for s in all_sites]
    dr = parse_date_range(start, end)
    keywords = [k.strip() for k in re.split(r"[\n,]", kw or "") if k.strip()]
    result = A.estimate(db, ids, dr, source=source, keywords=keywords, mode=mode) if all_sites else None
    return templates.TemplateResponse(request, "audience.html", {
        "request": request, "all_sites": all_sites, "chosen": set(ids),
        "range": dr, "source": source, "kw": kw or "", "mode": mode, "result": result,
    })


@router.get("/metrika")
def metrika_page(request: Request, site_id: int | None = None, start: str | None = None,
                 end: str | None = None, msg: str | None = None, db: Session = Depends(get_db)):
    from app.services import visits

    sites = _sites(db)
    site = _resolve_site(db, site_id)
    dr = parse_date_range(start, end)
    summary = None
    if site is not None:
        # Metrica data belongs to the domain, not to a GSC/Yandex property —
        # show the same-domain twin (any source) that actually holds the visits.
        vid = site.id
        d = domain_of(site.property_uri)
        mids = [sid for sid, uri in db.execute(select(Site.id, Site.property_uri)).all()
                if d and domain_of(uri) == d] or [site.id]
        if len(mids) > 1:
            from sqlalchemy import func

            from app.db.models import Visit
            counts = dict(db.execute(
                select(Visit.site_id, func.count()).where(Visit.site_id.in_(mids))
                .group_by(Visit.site_id)
            ).all())
            vid = max(mids, key=lambda i: (counts.get(i, 0), -i))
        summary = visits.summary(db, vid, dr)
    return templates.TemplateResponse(
        request,
        "metrika.html",
        {"request": request, "msg": msg, "sites": sites, "site": site,
         "range": dr, "summary": summary},
    )


@router.post("/ui/metrika/upload")
async def ui_metrika_upload(site_id: int = Form(...), file: UploadFile = File(...),
                            db: Session = Depends(get_db)):
    from app.services import visits

    if db.get(Site, site_id) is None:
        raise HTTPException(404, "site not found")
    try:
        n = visits.import_fileobj(db, site_id, file.file, file.filename or "upload")
        msg = f"Загружено визитов: {n}"
    except Exception as exc:  # noqa: BLE001
        msg = f"Ошибка: {exc}"
    finally:
        await file.close()
    return RedirectResponse(url=f"{BP}/metrika?site_id={site_id}&msg={quote(msg)}", status_code=303)


@router.get("/antifraud")
def antifraud_page(request: Request, site_id: int | None = None, start: str | None = None,
                   end: str | None = None, ratio: float = 10.0, min_impr: int = 100,
                   merge: int = 0, db: Session = Depends(get_db)):
    from app.services.antifraud import analyze

    sites = _sites(db)
    site = _resolve_site(db, site_id)
    dr = parse_date_range(start, end)
    result, merged = None, None
    if site is not None:
        ids = site.id
        if merge:
            mids = _same_domain_ids(db, site)
            if len(mids) > 1:
                ids = mids
                merged = {"count": len(mids), "domain": domain_of(site.property_uri)}
        result = analyze(db, ids, dr, ratio_threshold=ratio, min_impressions=min_impr)
    return templates.TemplateResponse(
        request,
        "antifraud.html",
        {
            "request": request, "sites": sites, "site": site, "range": dr,
            "ratio": ratio, "min_impr": min_impr, "result": result,
            "merge": bool(merge), "merged": merged,
        },
    )
