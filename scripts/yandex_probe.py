"""Probe Yandex Webmaster beta endpoints to capture their real response shapes.

    python scripts/yandex_probe.py prokompressor.ru

Uses the stored Yandex token. Prints the JSON of in-search samples,
query-analytics, and popular queries so the per-URL/indexing features can be
implemented against the actual format. Paste the output back to the assistant.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.credentials import get_cred  # noqa: E402

API = "https://api.webmaster.yandex.net/v4"


def show(title: str, resp: httpx.Response, limit: int = 1800) -> None:
    print(f"\n===== {title} =====")
    print("HTTP", resp.status_code)
    print(resp.text[:limit])


def main() -> None:
    needle = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    token = get_cred("yandex_wm_token", get_settings().yandex_wm_oauth_token)
    if not token:
        print("No Yandex token stored. Connect Yandex first.")
        return
    h = {"Authorization": f"OAuth {token}"}

    uid = httpx.get(f"{API}/user/", headers=h, timeout=30).json()["user_id"]
    print("user_id =", uid)
    hosts = httpx.get(f"{API}/user/{uid}/hosts/", headers=h, timeout=30).json().get("hosts", [])
    host = next((x for x in hosts if needle in (x.get("ascii_host_url", "")).lower()), None) or (hosts[0] if hosts else None)
    if not host:
        print("No hosts found.")
        return
    hid = host["host_id"]
    print("host_id =", hid, "|", host.get("ascii_host_url"))

    base = f"{API}/user/{uid}/hosts/{hid}"
    show("in-search/samples", httpx.get(f"{base}/search-urls/in-search/samples", headers=h, params={"limit": 5}, timeout=60))
    show("in-search/history", httpx.get(f"{base}/search-urls/in-search/history", headers=h, timeout=60))
    show(
        "query-analytics/list",
        httpx.post(
            f"{base}/query-analytics/list",
            headers=h,
            json={"offset": 0, "limit": 5, "device_type_indicator": "ALL", "text_indicator": "QUERY"},
            timeout=60,
        ),
    )
    show(
        "search-queries/popular",
        httpx.get(
            f"{base}/search-queries/popular",
            headers=h,
            params=[
                ("order_by", "TOTAL_CLICKS"),
                ("query_indicator", "TOTAL_SHOWS"),
                ("query_indicator", "TOTAL_CLICKS"),
                ("query_indicator", "AVG_SHOW_POSITION"),
            ],
            timeout=60,
        ),
    )


if __name__ == "__main__":
    main()
