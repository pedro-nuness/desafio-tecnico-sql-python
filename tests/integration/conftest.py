"""Integration fixtures: a real PostgreSQL migrated with Alembic (never create_all).

Set TEST_DATABASE_URL (see .env.example); tests are skipped when it is not set.
`docker compose up -d postgres` creates the `modernizer_test` database.
"""

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.database.engine import create_engine
from app.core.database.session import create_session_factory

ROOT = Path(__file__).parents[2]


def _test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set", allow_module_level=True)
    return url


@pytest.fixture(scope="session")
def migrated_database_url() -> str:
    url = _test_database_url()
    # Alembic's env.py calls asyncio.run(); run it out-of-process (the test loop is running).
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url},
        check=True,
    )
    return url


@pytest.fixture(scope="session")
async def engine(migrated_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(migrated_database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE modernization_history"))
    return create_session_factory(engine)
