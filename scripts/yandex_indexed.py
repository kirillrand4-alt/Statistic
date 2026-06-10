"""Short probe of just the Yandex 'pages in search' endpoints (fits one screen).

    python scripts/yandex_indexed.py prokompressor.ru

Prints in-search/samples and in-search/history only, so the output is small —
no scrolling needed. Paste it back to the assistant.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.credentials import get_cred  # noqa: E402

API = "https://api.webmaster.yandex.net/v4"


def main() -> None:
    needle = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    token = get_cred("yandex_wm_token", get_settings().yandex_wm_oauth_token)
    if not token:
        print("No Yandex token stored. Connect Yandex first.")
        return
    h = {"Authorization": f"OAuth {token}"}

    uid = httpx.get(f"{API}/user/", headers=h, timeout=30).json()["user_id"]
    hosts = httpx.get(f"{API}/user/{uid}/hosts/", headers=h, timeout=30).json().get("hosts", [])
    host = next((x for x in hosts if needle in (x.get("ascii_host_url", "")).lower()), None) or (hosts[0] if hosts else None)
    if not host:
        print("No hosts found.")
        return
    hid = host["host_id"]
    print("host:", host.get("ascii_host_url"), "|", hid)

    base = f"{API}/user/{uid}/hosts/{hid}/search-urls/in-search"
    r = httpx.get(f"{base}/samples", headers=h, params={"limit": 5}, timeout=60)
    print("\n=== in-search/samples ===", r.status_code)
    print(r.text[:1600])
    r2 = httpx.get(f"{base}/history", headers=h, timeout=60)
    print("\n=== in-search/history ===", r2.status_code)
    print(r2.text[:600])


if __name__ == "__main__":
    main()
