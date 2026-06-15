"""Small shared helpers used across services and providers."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit


def as_id_list(site_ids) -> list:
    """A single id or an iterable of ids -> a flat list (str/bytes treated as one)."""
    if isinstance(site_ids, Iterable) and not isinstance(site_ids, (str, bytes)):
        return list(site_ids)
    return [site_ids]


def normalize_url(url: str) -> str:
    """Normalize a URL for matching/dedup (NOT for display or API calls).

    Lowercases scheme + host, drops a leading ``www.``, removes the fragment,
    and strips a trailing slash (except for the root path). Path case and the
    query string are preserved, since paths can be case-sensitive.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((scheme, host, path, parts.query, ""))


def domain_of(property_uri: str) -> str:
    """Bare domain of a site property: 'sc-domain:x.ru' / 'https://x.ru/' -> 'x.ru'."""
    p = (property_uri or "").strip().lower()
    if p.startswith("sc-domain:"):
        host = p[len("sc-domain:"):]
    else:
        if "://" not in p:
            p = "http://" + p
        host = urlsplit(p).netloc
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def query_hash(text: str) -> str:
    """Stable hash for a query string (used as a unique key per site)."""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()
