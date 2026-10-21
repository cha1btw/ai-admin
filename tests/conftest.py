"""Test fixtures. Integration tests use a real Postgres (`make db`), unit tests need nothing."""

import os
from collections.abc import AsyncIterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.db import models  # noqa: F401  (registers tables)
from app.db.base import Base
from app.db.session import SessionFactory, make_engine, make_session_factory
from tests.helpers import ROOT

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://ai_admin:ai_admin@localhost:5435/ai_admin_test"
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integration" in str(item.path):
            item.add_marker(pytest.mark.db)


@pytest.fixture(scope="session")
def database_url() -> str:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    cfg.attributes["configure_logger"] = False
    try:
        command.upgrade(cfg, "head")
    except Exception as exc:
        pytest.exit(f"Test database is not reachable ({exc}). Run `make db` first.", returncode=2)
    return TEST_DATABASE_URL


@pytest.fixture
async def session_factory(database_url: str) -> AsyncIterator[SessionFactory]:
    engine = make_engine(database_url, null_pool=True)
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield make_session_factory(engine)
    await engine.dispose()
