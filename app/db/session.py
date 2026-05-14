from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.settings import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    echo=False,
    # pgbouncer transaction-pooling mode: server-side prepared statements are
    # invalidated on every connection rotation → disable the asyncpg cache (B-4)
    connect_args={"statement_cache_size": 0},
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


_direct_engine = None
_direct_session_factory = None


def _get_direct_engine():
    global _direct_engine
    if _direct_engine is None:
        _direct_engine = create_async_engine(
            settings.DATABASE_URL_DIRECT,
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=0,
            echo=False,
            connect_args={"statement_cache_size": 0},
        )
    return _direct_engine


def direct_session_factory() -> async_sessionmaker:
    global _direct_session_factory
    if _direct_session_factory is None:
        _direct_session_factory = async_sessionmaker(
            _get_direct_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _direct_session_factory()
