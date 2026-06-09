"""Application configuration.

Values come from environment variables / a ``.env`` file (see
``.env.example``). Runtime-editable overrides can additionally be stored in the
``AppSetting`` table; :func:`get_runtime_setting` reads those with an env
fallback so the owner can paste e.g. a Yandex token from the admin UI without a
redeploy.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = "production"
    secret_key: str = "change-me"
    database_url: str = "sqlite:///./data/seo.db"
    timezone: str = "Europe/Moscow"
    # Serve under a subpath behind a reverse proxy, e.g. "/stat". Empty = root.
    root_path: str = ""
    # Public base URL (e.g. https://parsercompressor.online) for OAuth redirects.
    # If empty, it is derived from the incoming request (Host + X-Forwarded-Proto).
    public_base_url: str = ""

    # Google Search Console
    gsc_auth_mode: str = "service_account"  # service_account | oauth
    gsc_service_account_file: str = "./secrets/gsc-sa.json"
    gsc_oauth_client_file: str = "./secrets/gsc-oauth-client.json"
    gsc_oauth_refresh_token: str = ""
    gsc_site_url: str = ""
    gsc_data_delay_days: int = 3

    # Yandex Webmaster
    yandex_wm_oauth_token: str = ""
    yandex_wm_user_id: str = ""
    yandex_wm_host_id: str = ""

    # Yandex Metrika
    yandex_metrika_oauth_token: str = ""
    yandex_metrika_counter_id: str = ""

    # Scheduler
    enable_scheduler: bool = True
    collect_cron_hour: int = 4
    collect_refetch_days: int = 5

    @property
    def base_path(self) -> str:
        """Normalized subpath prefix: "" or "/stat" (leading slash, no trailing)."""
        p = (self.root_path or "").strip()
        if not p:
            return ""
        if not p.startswith("/"):
            p = "/" + p
        return p.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    """Return cached settings parsed from the environment / ``.env``."""
    return Settings()
