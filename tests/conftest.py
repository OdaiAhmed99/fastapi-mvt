"""Fixtures for the library's own tests.

``TEST_DATABASE_URL`` switches the in-process tests to another database
(CI runs them against PostgreSQL too); by default a temporary SQLite file.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.pool import NullPool

from fastapi_mvt.db import Database
from tests import models  # noqa: F401  (registers the tables)


@pytest.fixture(scope="session")
def database(tmp_path_factory: pytest.TempPathFactory) -> Database:
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{(tmp_path_factory.mktemp('db') / 't.sqlite3').as_posix()}"
    db = Database(url, poolclass=NullPool)

    async def setup() -> None:
        await db.drop_all()
        await db.create_all()
        await db.dispose()

    asyncio.run(setup())
    return db


@pytest_asyncio.fixture
async def db(database: Database):
    """Each test runs in a transaction that is rolled back (the same mechanism projects get)."""
    async with database.engine.connect() as connection:
        transaction = await connection.begin()
        database._bind_override = connection
        try:
            yield database
        finally:
            database._bind_override = None
            await transaction.rollback()
    await database.dispose()


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent
