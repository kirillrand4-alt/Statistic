"""Database engine, session factory and declarative base.

SQLite by default; set ``DATABASE_URL`` to a ``postgresql+psycopg://`` URL to
use PostgreSQL. The schema is created with ``Base.metadata.create_all`` on
startup (good enough for v1; introduce Alembic later if migrations are needed).
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _make_engine():
    settings = get_settings()
    url = settings.database_url
    connect_args: dict = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        # Ensure the parent directory for a file-based SQLite DB exists.
        db_path = url.split("sqlite:///", 1)[-1]
        if db_path and db_path != ":memory:":
            Path(db_path).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )
    return create_engine(url, future=True, connect_args=connect_args)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    """WAL + busy_timeout so reads (dashboard) aren't blocked by writes (backfill)."""
    if engine.dialect.name == "sqlite":
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


def init_db() -> None:
    """Create all tables. Imports models for side effects (registration)."""
    from app.db import models  # noqa: F401
    from app.db.migrate import ensure_schema

    Base.metadata.create_all(bind=engine)
    ensure_schema(engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
