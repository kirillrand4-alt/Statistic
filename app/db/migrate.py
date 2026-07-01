"""Tiny additive migrations.

``Base.metadata.create_all`` creates missing *tables* but never alters existing
ones, so columns added to a model after its table already exists (e.g. on the
production DB) must be added by hand. This keeps that list in one place and runs
on startup. New tables (like ``hit``) need nothing here — create_all builds them
with all their columns.
"""
from __future__ import annotations

from sqlalchemy import inspect

# (table, column, column DDL) — ADD COLUMN is valid on both SQLite and Postgres.
_ADDED_COLUMNS = [
    ("visit", "extra", "TEXT"),
    ("project", "favorite_goals", "TEXT"),
    ("serp_result", "snippet", "TEXT"),
    # Wordstat frequency match type (broad | phrase | exact | order); existing rows
    # were broad-match. Uniqueness stays via a match-aware query_hash, so no need to
    # touch the unique constraint — this column is just a label for filter/display.
    ("wordstat_history", "match_type", "VARCHAR(8) DEFAULT 'broad'"),
    ("wordstat_series", "match_type", "VARCHAR(8) DEFAULT 'broad'"),
]


def ensure_schema(engine) -> None:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for table, column, ddl in _ADDED_COLUMNS:
        if table not in tables:
            continue  # create_all() will create it with the column already present
        if column not in {c["name"] for c in insp.get_columns(table)}:
            with engine.begin() as conn:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
