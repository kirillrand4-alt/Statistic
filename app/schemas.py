"""Pydantic request/response models for the JSON API."""
from __future__ import annotations

from pydantic import BaseModel


class ProjectCreate(BaseModel):
    name: str
    site_id: int


class SiteCreate(BaseModel):
    source_code: str = "gsc"
    property_uri: str
    display_name: str | None = None
    external_host_id: str | None = None


class UrlUpload(BaseModel):
    urls: list[str]
