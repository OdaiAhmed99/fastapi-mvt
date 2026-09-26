"""pytest plugin, active automatically once fastapi-mvt is installed.

Fixtures (need ``pytest-asyncio``: ``pip install "fastapi-mvt[test]"``):

``db``
    The project's ``Database``. The test runs inside a transaction that is
    rolled back afterwards, so tests never see each other's data.
``client``
    An ``httpx.AsyncClient`` for the app, sharing the test's transaction.

Every ``async def`` test in a project gets ``db`` automatically, so no test
can leak data into the next one by forgetting to ask for it. Opt a test out
with ``@pytest.mark.no_db``.

The test database is separate from your dev database:

* SQLite: a temporary file.
* PostgreSQL: ``<name>_test``, created and dropped like Django does.
* Anything else, or to choose yourself: set ``TEST_DATABASE_URL``.

It is built by running your migrations (so they're tested too), or with
``create_all()`` if the project has none yet.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import os
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Optional

import pytest

try:
    import pytest_asyncio
except ImportError:  # pragma: no cover
    pytest_asyncio = None  # type: ignore[assignment]


@functools.lru_cache(maxsize=None)
def _project() -> Any:
    from fastapi_mvt.project import find_project

    return find_project()


def pytest_configure(config: Any) -> None:
    config.addinivalue_line("markers", "no_db: don't wrap this async test in a rolled-back database transaction")


@pytest.fixture(autouse=True)
def _mvt_isolate_async_tests(request: Any) -> None:
    """Give every async test the `db` fixture (per-test rollback) inside a fastapi-mvt project."""
    function = getattr(request, "function", None)
    if (
        function is None
        or not inspect.iscoroutinefunction(function)
        or "db" in request.fixturenames
        or request.node.get_closest_marker("no_db")
        or _project() is None
    ):
        return
    request.getfixturevalue("db")


def _test_url(database: Any, tmp_dir: Path) -> tuple[Any, Optional[Any]]:
    """Return (test URL, admin URL used to create/drop it or None)."""
    from sqlalchemy.engine import make_url

    from fastapi_mvt.db import to_async_url

    if os.environ.get("TEST_DATABASE_URL"):
        return to_async_url(os.environ["TEST_DATABASE_URL"]), None
    url = database.url
    backend = url.get_backend_name()
    if backend == "sqlite":
        return make_url(f"sqlite+aiosqlite:///{(tmp_dir / 'test.sqlite3').as_posix()}"), None
    if backend == "postgresql":
        return url.set(database=f"{url.database}_test"), url.set(database="postgres")
    raise pytest.UsageError(f"Set TEST_DATABASE_URL to run tests against {backend}.")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _postgres(admin_url: Any, statement: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(statement))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def mvt_project() -> Any:
    project = _project()
    if project is None:
        pytest.skip("Not inside a fastapi-mvt project (no [tool.fastapi-mvt] in pyproject.toml).")
    from fastapi_mvt.project import activate

    activate(project)
    return project


@pytest.fixture(scope="session")
def mvt_database(mvt_project: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """Create the test database once per test run."""
    from sqlalchemy.pool import NullPool

    from fastapi_mvt.migrations import commands
    from fastapi_mvt.project import load_database, load_project_models

    load_project_models(mvt_project)
    database = load_database(mvt_project)
    original_url, original_options = database.url, dict(database._engine_options)
    test_url, admin_url = _test_url(database, tmp_path_factory.mktemp("fastapi_mvt"))

    if admin_url is not None:
        name = test_url.database
        _run(_postgres(admin_url, f'DROP DATABASE IF EXISTS "{name}"'))
        _run(_postgres(admin_url, f'CREATE DATABASE "{name}"'))

    # NullPool: each test's event loop gets fresh connections.
    database.configure(test_url, poolclass=NullPool)
    versions = mvt_project.migrations_path / "versions"
    try:
        if versions.is_dir() and any(versions.glob("*.py")):
            commands.migrate(mvt_project, check_models=False)
            if commands.has_unmigrated_changes(mvt_project, against_history=False):
                pytest.exit(
                    "Your models have changes that are not in a migration, so the test database would not "
                    "match them. Run `python manage.py makemigrations` first.",
                    returncode=4,
                )
        else:
            _run(_create_all(database))
        yield database
    finally:
        _run(database.dispose())
        if admin_url is not None:
            _run(_postgres(admin_url, f'DROP DATABASE IF EXISTS "{test_url.database}" WITH (FORCE)'))
        database.configure(original_url, **original_options)


async def _create_all(database: Any) -> None:
    try:
        await database.create_all()
    finally:
        await database.dispose()


if pytest_asyncio is not None:

    @pytest_asyncio.fixture
    async def db(mvt_database: Any) -> AsyncIterator[Any]:
        database = mvt_database
        async with database.engine.connect() as connection:
            transaction = await connection.begin()
            database._bind_override = connection
            try:
                yield database
            finally:
                database._bind_override = None
                if transaction.is_active:
                    await transaction.rollback()
        await database.dispose()

    @pytest_asyncio.fixture
    async def client(db: Any, mvt_project: Any) -> AsyncIterator[Any]:
        import httpx

        from fastapi_mvt.project import load_app

        app = load_app(mvt_project)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
            yield c

else:  # pragma: no cover

    @pytest.fixture
    def db() -> None:
        raise pytest.UsageError('The db fixture needs pytest-asyncio: pip install "fastapi-mvt[test]"')

    @pytest.fixture
    def client() -> None:
        raise pytest.UsageError('The client fixture needs pytest-asyncio: pip install "fastapi-mvt[test]"')
