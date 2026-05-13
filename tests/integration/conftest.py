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


@pytest.fixture(scope="session", autouse=True)
def apply_migrations(alembic_config):
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
