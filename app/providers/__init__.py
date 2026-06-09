"""Provider registry.

Services and the scheduler resolve a provider by source code and never import
vendor modules directly. Tests inject a ``MockProvider`` via
:func:`register_override`.
"""
from __future__ import annotations

from app.providers.base import SearchDataProvider

_OVERRIDES: dict[str, SearchDataProvider] = {}
_CACHE: dict[str, SearchDataProvider] = {}


def register_override(code: str, provider: SearchDataProvider) -> None:
    _OVERRIDES[code] = provider


def clear_overrides() -> None:
    _OVERRIDES.clear()


def get_provider(code: str) -> SearchDataProvider:
    if code in _OVERRIDES:
        return _OVERRIDES[code]
    if code in _CACHE:
        return _CACHE[code]

    if code == "gsc":
        from app.providers.gsc import GSCProvider

        provider: SearchDataProvider = GSCProvider()
    elif code == "yandex_webmaster":
        raise NotImplementedError("Yandex Webmaster provider arrives in Phase 2")
    elif code == "yandex_metrika":
        raise NotImplementedError("Yandex Metrika provider arrives in Phase 3")
    else:
        raise KeyError(f"Unknown provider code: {code!r}")

    _CACHE[code] = provider
    return provider
