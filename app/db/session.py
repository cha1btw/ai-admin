from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

SessionFactory = async_sessionmaker[AsyncSession]


def make_engine(database_url: str, *, null_pool: bool = False) -> AsyncEngine:
    kwargs: dict = {"pool_pre_ping": True}
    if null_pool:
        kwargs = {"poolclass": NullPool}
    return create_async_engine(database_url, **kwargs)


def make_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)


async def session_scope(factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session
