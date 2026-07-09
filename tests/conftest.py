"""Test fixtures: isolated SQLite DB + MockProvider injected for the gsc source."""
from __future__ import annotations

import os
import tempfile

# Configure environment BEFORE importing the app (settings are cached).
_TMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["GSC_SITE_URL"] = ""
os.environ["PAGE_CACHE_TTL"] = "0"  # disable HTML page cache so tests see live data
os.environ["OBZVON_USERS"] = "test:test,seller:sell"  # Basic auth of the obzvon app
os.environ["OBZVON_ADMINS"] = "test"  # only "test" may upload/clear obzvon bases

import pytest  # noqa: E402

from app.bootstrap import ensure_sources  # noqa: E402
from app.db.base import Base, SessionLocal, engine  # noqa: E402
from app.db.models import Project, ProjectUrl, Site  # noqa: E402
from app.providers import clear_overrides, register_override  # noqa: E402
from app.providers.mock import DEFAULT_PAGES, MockProvider  # noqa: E402
from app.utils import normalize_url  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    register_override("gsc", MockProvider())
    yield
    clear_overrides()


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def site(db):
    gsc = ensure_sources(db)["gsc"]
    s = Site(source_id=gsc.id, property_uri="sc-domain:example.com", display_name="demo")
    db.add(s)
    db.commit()
    return s


@pytest.fixture()
def project(db, site):
    p = Project(name="P", site_id=site.id)
    db.add(p)
    db.commit()
    for u in DEFAULT_PAGES:
        db.add(ProjectUrl(project_id=p.id, url=u, normalized_url=normalize_url(u)))
    db.commit()
    return p
