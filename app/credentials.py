"""Encrypted credential store backed by the AppSetting table.

Lets the UI save API credentials (pasted by the owner) that providers read at
runtime — no .env editing or restart. Values are encrypted at rest with a key
derived from SECRET_KEY.
"""
from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models import AppSetting

logger = logging.getLogger(__name__)


def _box() -> Fernet:
    digest = hashlib.sha256(get_settings().secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def set_cred(key: str, value: str) -> None:
    token = _box().encrypt(value.encode("utf-8")).decode("ascii")
    db = SessionLocal()
    try:
        row = db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=token))
        else:
            row.value = token
        db.commit()
    finally:
        db.close()


def get_cred(key: str, default: str | None = None) -> str | None:
    db = SessionLocal()
    try:
        row = db.get(AppSetting, key)
    finally:
        db.close()
    if row is None or row.value is None:
        return default
    try:
        return _box().decrypt(row.value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        logger.warning("Could not decrypt credential %r (SECRET_KEY changed?)", key)
        return default
