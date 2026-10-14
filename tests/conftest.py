"""Test fixtures. Requires a running Postgres (see README Phase 1 section).

Tests run against a dedicated "<database>_test" database, created on demand, so
they never touch (or drop_all) the dev/compose database that `alembic upgrade head`
manages.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from api.main import app
from common.config import get_settings
from common.db import get_db
from common.models import Base


def _test_db_url():
    url = make_url(get_settings().database_url)
    return url.set(database=f"{url.database}_test")


def _ensure_test_database(test_url) -> None:
    admin_url = make_url(get_settings().database_url).set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": test_url.database},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{test_url.database}"'))
    finally:
        admin_engine.dispose()


TEST_DB_URL = _test_db_url()
_ensure_test_database(TEST_DB_URL)

TEST_ENGINE = create_engine(TEST_DB_URL, pool_pre_ping=True, future=True)
TestingSessionLocal = sessionmaker(bind=TEST_ENGINE, autoflush=False, autocommit=False, future=True)


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    with TEST_ENGINE.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
    Base.metadata.create_all(TEST_ENGINE)
    yield
    Base.metadata.drop_all(TEST_ENGINE)


@pytest.fixture(autouse=True)
def _clean_tables():
    yield
    with TEST_ENGINE.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture
def db_session():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def fresh_from_db(model, id_):
    """For tests proving something was actually *committed* (not just
    flushed/mutated in-memory) by reading it back through a brand new
    session — same reasoning as test_scheduler_reaper.py's two-session
    pattern. ALWAYS use this (or your own try/finally) instead of a bare
    `TestingSessionLocal().get(...)`: an unclosed session leaves an
    idle-in-transaction connection holding a lock, which can hang
    `Base.metadata.drop_all()` in `_create_schema`'s teardown at the end of
    the whole test session — confirmed live, and the hang is intermittent
    (depends on whether Python's cyclic GC happens to collect the orphaned
    Session first), which makes it a nasty one to debug blind later."""
    session = TestingSessionLocal()
    try:
        return session.get(model, id_)
    finally:
        session.close()


@pytest.fixture
def client():
    def _override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
