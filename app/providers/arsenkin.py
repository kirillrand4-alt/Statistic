"""ARSENKIN TOOLS API client — the ``check-top`` (ТОП-10 SERP) tool.

Async task API:
  POST {base}/set   {"tools_name":"check-top","data":{queries,se,depth,...}} -> {"task_id":N}
  POST {base}/check {"task_id":N}
  POST {base}/get   {"task_id":N} -> {"code":"TASK_RESULT","result":{request,result}}
  POST {base}/info  {"query":"limits"|"status"}

Documented limits: <=5 concurrent tasks, queue <=50, <=30 requests/min across
ALL endpoints (HTTP 429 / {"code":"429"} otherwise). Auth: ``Bearer <token>``.
Everything here is config-driven (base URL, rate, timeouts) so it can be tuned.
"""
from __future__ import annotations

import time
from collections import deque

import httpx

DEFAULT_BASE = "https://arsenkin.ru/api/tools"

# arsenkin se.type -> human label (engine + device)
SE_LABELS = {
    1: "Яндекс XML", 2: "Яндекс", 3: "Яндекс (моб)",
    11: "Google", 12: "Google (моб)", 20: "YouTube", 21: "YouTube (моб)",
}
# default region per engine family for "Москва"
DEFAULT_REGION = {1: 213, 2: 213, 3: 213, 11: 1011969, 12: 1011969, 20: "RU|ru", 21: "RU|ru"}


class RateLimiter:
    """Allow at most ``max_per_min`` calls in any trailing 60 s window."""

    def __init__(self, max_per_min: int = 28):
        self.max = max(1, max_per_min)
        self._calls: deque[float] = deque()

    def wait(self) -> None:
        now = time.monotonic()
        while self._calls and now - self._calls[0] >= 60:
            self._calls.popleft()
        if len(self._calls) >= self.max:
            time.sleep(max(0.1, 60 - (now - self._calls[0]) + 0.2))
            return self.wait()
        self._calls.append(time.monotonic())


def _is_429(status_code: int, data: dict) -> bool:
    return status_code == 429 or str((data or {}).get("code")) == "429"


class Arsenkin:
    def __init__(self, token: str, base: str = DEFAULT_BASE, max_per_min: int = 28,
                 timeout: int = 90, retries: int = 6):
        self.token = token
        self.base = base.rstrip("/")
        self.rl = RateLimiter(max_per_min)
        self.timeout = timeout
        self.retries = retries

    def _post(self, path: str, body: dict) -> dict:
        headers = {"Authorization": f"Bearer {self.token}", "Content-type": "application/json"}
        for attempt in range(self.retries):
            self.rl.wait()
            try:
                r = httpx.post(f"{self.base}/{path}", json=body, headers=headers, timeout=self.timeout)
            except Exception as exc:  # noqa: BLE001 — network hiccup, retry
                if attempt == self.retries - 1:
                    return {"status": "Error", "error": f"{exc.__class__.__name__}: {exc}"}
                time.sleep(3 * (attempt + 1))
                continue
            try:
                data = r.json()
            except Exception:  # noqa: BLE001
                data = {"status": "Error", "code": str(r.status_code), "raw": r.text[:300]}
            if _is_429(r.status_code, data):
                time.sleep(5 * (attempt + 1))
                continue
            return data
        return {"status": "Error", "error": "429 retries exhausted"}

    def set_task(self, queries, se, depth=10, is_snippet=False, noreask=False) -> dict:
        return self._post("set", {"tools_name": "check-top", "data": {
            "queries": list(queries), "se": list(se), "depth": depth,
            "is_snippet": bool(is_snippet), "noreask": bool(noreask)}})

    def check(self, task_id) -> dict:
        return self._post("check", {"task_id": task_id})

    def get(self, task_id) -> dict:
        return self._post("get", {"task_id": task_id})

    def limits(self) -> dict:
        return self._post("info", {"query": "limits"})

    def running(self) -> dict:
        return self._post("info", {"query": "status"})

    def delete(self, task_id) -> dict:
        return self._post("tasks", {"action": "delete", "task_id": task_id})


def _title_snippet(snippets: dict, url: str):
    """(title, snippet) for a URL from the result's snippets block, which is
    either {url: [{title,snippet}]} or {url: {"1": {title,snippet}}}."""
    sn = (snippets or {}).get(url)
    item = None
    if isinstance(sn, list) and sn:
        item = sn[0]
    elif isinstance(sn, dict):
        item = next(iter(sn.values()), None)
    if isinstance(item, dict):
        return item.get("title"), item.get("snippet")
    return None, None


def parse_result(payload: dict):
    """Yield {query, se, region, position, url, title, snippet} rows from a
    ``get`` TASK_RESULT payload. ``collect[query_index][se_index]`` is the ranked
    URL list; the se order matches ``request.ss``."""
    res = (payload or {}).get("result") or {}
    req = res.get("request") or {}
    inner = res.get("result") or {}
    queries = req.get("queries") or []
    ss = req.get("ss") or []  # [{"ss": type, "region": id}]
    snippets = inner.get("snippets") or {}
    for qi, per_se in enumerate(inner.get("collect") or []):
        query = queries[qi] if qi < len(queries) else None
        for si, urls in enumerate(per_se or []):
            se = ss[si] if si < len(ss) else {}
            for pos, url in enumerate(urls or [], 1):
                title, snippet = _title_snippet(snippets, url)
                yield {"query": query, "se": se.get("ss"), "region": se.get("region"),
                       "position": pos, "url": url, "title": title, "snippet": snippet}


def se_for(types, yandex_region=213, google_region=1011969) -> list[dict]:
    """Build the ``se`` list: Yandex engines (1/2/3) use the Yandex region id,
    Google engines (11/12) use the Google region id (different id schemes)."""
    out = []
    for t in types:
        t = int(t)
        if t in (1, 2, 3):
            r = yandex_region
        elif t in (11, 12):
            r = google_region
        else:
            r = DEFAULT_REGION.get(t)
        out.append({"type": t, "region": r})
    return out


def is_done(payload: dict) -> bool:
    """True if a ``get`` payload carries the finished result."""
    return str((payload or {}).get("code")) == "TASK_RESULT"


# /check reports {"code":"TASK_STATUS","status":"process","progress":N} while
# running. /get can return TASK_RESULT before the SERP is actually collected, so
# readiness is taken from /check: 100% progress, a result/done code, or any
# non-running status.
_DONE_CODES = {"TASK_RESULT", "TASK_DONE", "DONE", "TASK_COMPLETE", "COMPLETE", "TASK_OK", "READY"}
_RUNNING = {"process", "processing", "in_progress", "queue", "queued", "wait", "waiting",
            "pending", "new", "created", "start", "started", "running", "work", "working"}


def check_done(payload: dict) -> bool:
    """Tolerant 'is the task finished?' read of a /check response."""
    if not payload:
        return False
    if str(payload.get("code", "")).upper() in _DONE_CODES:
        return True
    prog = payload.get("progress", payload.get("percent"))
    try:
        if prog is not None and float(str(prog).replace("%", "").strip()) >= 100:
            return True
    except (TypeError, ValueError):
        pass
    status = str(payload.get("status", "")).strip().lower()
    return bool(status) and status not in _RUNNING  # any non-running status = done
