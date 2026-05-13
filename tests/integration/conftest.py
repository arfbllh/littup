"""
Integration test fixtures.

Requires a running Postgres 16 + pgvector instance.
Set TEST_DATABASE_URL in environment or .env:

    TEST_DATABASE_URL=postgresql+asyncpg://littup:littup@localhost:5432/littup_test
    pytest tests/integration/
"""

import asyncio
import os

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://littup:littup@localhost:5432/littup_test",
)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def alembic_config():
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    return cfg


def _terminate_stale_backends() -> None:
    """Kill any leftover backends on the test DB before we start.

    A previous pytest run that was killed (Ctrl-C, OOM, SIGKILL) leaves
    asyncpg connections in `idle in transaction` on the server. They hold
    row/table locks that wedge our cleanup `TRUNCATE ... CASCADE` forever.
    """
    import re
    from urllib.parse import urlparse

    import asyncpg

    # Strip the SQLAlchemy "+driver" suffix for asyncpg.connect().
    libpq_url = re.sub(r"^postgresql\+[^:]+://", "postgresql://", TEST_DB_URL)
    parsed = urlparse(libpq_url)
    dbname = (parsed.path or "/").lstrip("/") or "postgres"
    admin_url = libpq_url.replace(f"/{dbname}", "/postgres", 1)

    async def _run() -> None:
        conn = await asyncpg.connect(admin_url)
        try:
            await conn.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM   pg_stat_activity
                WHERE  datname = $1 AND pid <> pg_backend_pid()
                """,
                dbname,
            )
        finally:
            await conn.close()

    try:
        asyncio.new_event_loop().run_until_complete(_run())
    except Exception:
        # Best-effort — if the admin DB is unreachable we'll surface the
        # underlying wedge via the lock_timeout in cleanup_documents_and_jobs.
        pass


@pytest.fixture(scope="session", autouse=True)
def apply_migrations(alembic_config):
    _terminate_stale_backends()
    command.upgrade(alembic_config, "head")
    yield
    command.downgrade(alembic_config, "base")


@pytest_asyncio.fixture(scope="session")
async def db_engine():
    # NullPool ensures every checkout creates a brand-new asyncpg connection,
    # so no async protocol state leaks between tests.
    engine = create_async_engine(TEST_DB_URL, echo=False, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.close()


@pytest_asyncio.fixture
async def test_session_factory(db_engine):
    """Session factory bound to the test engine for fixtures that need their
    own sessions (the app endpoints use a singleton in app.db.session, which
    we override via dependency injection or by patching it)."""
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def tmp_uploads_dir(tmp_path, monkeypatch):
    """Redirect uploads + page images to a per-test tmp directory and align
    the app's settings + global engine for the duration of the test."""
    from app import settings as settings_module

    upload_dir = tmp_path / "uploads"
    page_dir = tmp_path / "page_images"
    upload_dir.mkdir(parents=True, exist_ok=True)
    page_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings_module.settings, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(settings_module.settings, "PAGE_IMAGE_DIR", str(page_dir))
    return upload_dir


@pytest_asyncio.fixture
async def cleanup_documents_and_jobs(test_session_factory):
    """Truncate app.documents (cascades to events) + jobs.jobs after the test
    so we don't poison the shared integration DB for sibling test files."""
    yield
    from sqlalchemy import text as _sql

    async with test_session_factory() as s:
        # Fail fast if a stale backend is holding a conflicting lock instead
        # of hanging forever on AccessExclusiveLock acquisition.
        await s.execute(_sql("SET LOCAL lock_timeout = '5s'"))
        await s.execute(_sql("TRUNCATE app.documents CASCADE"))
        await s.execute(_sql("TRUNCATE jobs.jobs CASCADE"))
        await s.execute(_sql("TRUNCATE jobs.job_history CASCADE"))
        await s.commit()


@pytest_asyncio.fixture
async def app_client(
    db_engine, test_session_factory, tmp_uploads_dir, cleanup_documents_and_jobs, monkeypatch
):
    """An httpx.AsyncClient against the FastAPI app, with the app's DB session
    dependency rebound to the test engine and an event-emission loop short
    enough to keep SSE tests fast."""
    from httpx import ASGITransport, AsyncClient

    from app import settings as settings_module
    from app.api.routes.documents import get_ingest_service
    from app.db import session as app_session
    from app.ingest.service import IngestService
    from app.main import create_app

    monkeypatch.setattr(app_session, "async_session_factory", test_session_factory)
    monkeypatch.setattr(settings_module.settings, "SSE_POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(settings_module.settings, "SSE_KEEPALIVE_SECONDS", 0.5)
    monkeypatch.setattr(settings_module.settings, "SSE_MAX_STREAM_SECONDS", 5.0)

    app = create_app()

    async def _override_session():
        async with test_session_factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    async def _override_service():
        async with test_session_factory() as s:
            try:
                yield IngestService(s)
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    from app.db.session import get_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_ingest_service] = _override_service

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
def sample_pdf_bytes() -> bytes:
    """A tiny PDF whose body bytes vary per test invocation so its SHA256
    is unique across the shared integration DB."""
    import uuid

    unique = uuid.uuid4().hex.encode()
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
        b"xref\n0 4\n"
        b"0000000000 65535 f \n"
        b"0000000010 00000 n \n"
        b"0000000053 00000 n \n"
        b"0000000098 00000 n \n"
        b"trailer<</Size 4/Root 1 0 R>>\n"
        b"startxref\n149\n%%EOF\n"
        b"%% " + unique + b"\n"
    )
