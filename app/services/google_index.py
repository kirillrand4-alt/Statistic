"""Per-page Google index status (URL Inspection API) and on-demand index
submission (Indexing API).

Both APIs work **one URL per request**, so a list is processed in a background
thread that writes progress + results to a JSON file the page polls (the same
"refresh in a moment" idiom as the Yandex snapshot capture). Only Google Search
Console sites are supported; the service account must be a verified owner of the
property (and, for submission, the Indexing API must be enabled in the Cloud
project — it is officially meant for JobPosting/BroadcastEvent pages).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.db.base import SessionLocal
from app.db.models import Site
from app.providers import get_provider
from app.utils import domain_of, normalize_url

logger = logging.getLogger(__name__)

# Where per-site result files live (overridable for tests).
DATA_DIR = Path(os.environ.get(
    "GOOGLE_INDEX_DIR", str(Path(__file__).resolve().parents[2] / "data" / "google_index")))

MAX_INSPECT = 2000  # URL Inspection API daily quota (per property)
MAX_SUBMIT = 200    # Indexing API default daily quota (per project)

# In-process guard so the same (kind, site) batch can't run twice at once.
_LOCK = threading.Lock()
_RUNNING: set[tuple[str, int]] = set()


# ----- support checks -----
def _has_cap(site: Site, cap: str) -> bool:
    try:
        return cap in get_provider(site.source.code).capabilities
    except Exception:  # noqa: BLE001
        return False


def supports_inspection(site: Site) -> bool:
    return _has_cap(site, "url_inspection")


def supports_submit(site: Site) -> bool:
    return _has_cap(site, "index_submit")


# ----- result files -----
def _result_path(kind: str, site_id: int) -> Path:
    return DATA_DIR / f"{kind}_site{site_id}.json"


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)  # atomic so a polling read never sees a half-written file


def load_result(kind: str, site_id: int) -> dict | None:
    path = _result_path(kind, site_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def is_running(kind: str, site_id: int) -> bool:
    with _LOCK:
        return (kind, site_id) in _RUNNING


# ----- url prep -----
def _prep_urls(site: Site, urls, cap: int) -> list[str]:
    """De-dupe and resolve bare paths against the site domain. Google needs the
    exact absolute URL, so we keep the candidate but de-dupe on its normal form."""
    base = domain_of(site.property_uri)
    out, seen = [], set()
    for raw in urls:
        u = (raw or "").strip()
        if not u:
            continue
        cand = f"https://{base}{u}" if u.startswith("/") and base else u
        key = normalize_url(cand)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(cand)
        if len(out) >= cap:
            break
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _http_error(exc: BaseException) -> tuple[int | None, str]:
    """(status, short message) for a googleapiclient HttpError, else (None, str)."""
    try:
        from googleapiclient.errors import HttpError
    except Exception:  # pragma: no cover
        return None, str(exc)
    if isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", None)
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = None
        reason = ""
        try:
            payload = json.loads(exc.content.decode("utf-8"))
            reason = payload.get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            reason = getattr(exc, "reason", "") or ""
        return status, (reason or f"HTTP {status}")[:300]
    return None, str(exc)[:300]


# ----- inspection -----
def run_inspect(site_id: int, raw_urls) -> dict:
    """Inspect every URL synchronously; persist progress as it goes. Returns the
    final state (also stored to the result file)."""
    db = SessionLocal()
    state: dict = {
        "kind": "inspect", "site_id": site_id, "running": True,
        "started": _now(), "finished": None, "total": 0, "done": 0,
        "in_count": 0, "out_count": 0, "error": None, "rows": [],
    }
    try:
        site = db.get(Site, site_id)
        if site is None:
            state.update(running=False, error="Сайт не найден.", finished=_now())
            _write(_result_path("inspect", site_id), state)
            return state
        provider = get_provider(site.source.code)
        site_url = site.property_uri
        urls = _prep_urls(site, raw_urls, MAX_INSPECT)
        state["total"] = len(urls)
        _write(_result_path("inspect", site_id), state)
        for i, u in enumerate(urls, 1):
            try:
                res = provider.inspect_url(site_url, u)
                idx = res.get("indexStatusResult", {}) or {}
                verdict = idx.get("verdict")
                row = {
                    "input": u,
                    "indexed": verdict == "PASS",
                    "verdict": verdict,
                    "coverage": idx.get("coverageState"),
                    "last_crawl": idx.get("lastCrawlTime"),
                    "canonical": idx.get("googleCanonical"),
                    "robots": idx.get("robotsTxtState"),
                    "fetch": idx.get("pageFetchState"),
                    "error": None,
                }
                if row["indexed"]:
                    state["in_count"] += 1
                else:
                    state["out_count"] += 1
            except Exception as exc:  # noqa: BLE001
                status, msg = _http_error(exc)
                row = {"input": u, "indexed": None, "verdict": None, "coverage": None,
                       "last_crawl": None, "canonical": None, "robots": None,
                       "fetch": None, "error": msg}
                state["out_count"] += 1
                state["rows"].append(row)
                state["done"] = i
                if status == 429:  # daily quota / rate cap hit — stop the batch
                    state["error"] = f"Лимит запросов Google исчерпан: {msg}"
                    break
                _write(_result_path("inspect", site_id), state)
                continue
            state["rows"].append(row)
            state["done"] = i
            if i % 3 == 0:
                _write(_result_path("inspect", site_id), state)
        # not-indexed / errors first, like the Yandex check
        state["rows"].sort(key=lambda r: (r["indexed"] is True, r["input"]))
        state["running"] = False
        state["finished"] = _now()
        _write(_result_path("inspect", site_id), state)
        return state
    except Exception:  # noqa: BLE001
        logger.exception("Google URL inspection failed for site %s", site_id)
        state.update(running=False, finished=_now(),
                     error=state["error"] or "Внутренняя ошибка проверки.")
        _write(_result_path("inspect", site_id), state)
        return state
    finally:
        db.close()


# ----- submission -----
def run_submit(site_id: int, raw_urls, type_: str = "URL_UPDATED") -> dict:
    db = SessionLocal()
    state: dict = {
        "kind": "submit", "site_id": site_id, "running": True, "type": type_,
        "started": _now(), "finished": None, "total": 0, "done": 0,
        "ok": 0, "failed": 0, "error": None, "rows": [],
    }
    try:
        site = db.get(Site, site_id)
        if site is None:
            state.update(running=False, error="Сайт не найден.", finished=_now())
            _write(_result_path("submit", site_id), state)
            return state
        provider = get_provider(site.source.code)
        urls = _prep_urls(site, raw_urls, MAX_SUBMIT)
        state["total"] = len(urls)
        _write(_result_path("submit", site_id), state)
        for i, u in enumerate(urls, 1):
            try:
                resp = provider.request_indexing(u, type_)
                meta = (resp or {}).get("urlNotificationMetadata", {}) or {}
                latest = meta.get("latestUpdate", {}) or {}
                row = {"url": u, "ok": True, "notify_time": latest.get("notifyTime"), "error": None}
                state["ok"] += 1
            except Exception as exc:  # noqa: BLE001
                status, msg = _http_error(exc)
                row = {"url": u, "ok": False, "notify_time": None, "error": msg}
                state["failed"] += 1
                state["rows"].append(row)
                state["done"] = i
                if status == 429:
                    state["error"] = f"Дневная квота Indexing API исчерпана: {msg}"
                    break
                _write(_result_path("submit", site_id), state)
                continue
            state["rows"].append(row)
            state["done"] = i
            if i % 3 == 0:
                _write(_result_path("submit", site_id), state)
        state["running"] = False
        state["finished"] = _now()
        _write(_result_path("submit", site_id), state)
        return state
    except Exception:  # noqa: BLE001
        logger.exception("Google index submission failed for site %s", site_id)
        state.update(running=False, finished=_now(),
                     error=state["error"] or "Внутренняя ошибка отправки.")
        _write(_result_path("submit", site_id), state)
        return state
    finally:
        db.close()


# ----- background launchers -----
def _spawn(kind: str, site_id: int, target, *args) -> tuple[bool, str]:
    key = (kind, site_id)
    with _LOCK:
        if key in _RUNNING:
            return False, "Уже выполняется — обновите страницу через минуту."
        _RUNNING.add(key)

    def _runner():
        try:
            target(site_id, *args)
        finally:
            with _LOCK:
                _RUNNING.discard(key)

    threading.Thread(target=_runner, daemon=True).start()
    return True, ""


def start_inspect(site_id: int, urls) -> tuple[bool, str]:
    ok, msg = _spawn("inspect", site_id, run_inspect, urls)
    return ok, (msg or "Проверка индексации в Google запущена — обновите страницу через минуту.")


def start_submit(site_id: int, urls, type_: str = "URL_UPDATED") -> tuple[bool, str]:
    ok, msg = _spawn("submit", site_id, run_submit, urls, type_)
    return ok, (msg or "Отправка на индексацию запущена — обновите страницу через минуту.")
