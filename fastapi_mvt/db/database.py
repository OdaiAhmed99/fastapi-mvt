"""Engine, sessions and transactions.

One :class:`Database` object per project owns the async engine. Code never
passes sessions around: the active session lives in a context variable and
is resolved like this, for every ORM call:

1. Inside an HTTP request (after ``db.init_app(app)``) there is one session
   per request. It commits just before the response is sent, or rolls back if
   the endpoint raised or the response is an error (status >= 400).
2. Inside ``async with atomic():`` or ``async with db.session():`` that
   block's session is used.
3. Anywhere else (shell, scripts, background tasks, websockets) each ORM call
   runs in its own short transaction and commits immediately, like Django's
   autocommit mode.

``current_session()`` hands you the real ``AsyncSession`` whenever you want
plain SQLAlchemy.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from fastapi_mvt.db.exceptions import SessionError, install_exception_handlers

logger = logging.getLogger("fastapi_mvt.db")

_ASYNC_DRIVERS = {
    "sqlite": "sqlite+aiosqlite",
    "postgresql": "postgresql+asyncpg",
    "postgres": "postgresql+asyncpg",
    "mysql": "mysql+aiomysql",
    "mariadb": "mariadb+aiomysql",
}


def to_async_url(url: str | URL) -> URL:
    """Return *url* with an async driver, e.g. ``postgresql://`` -> ``postgresql+asyncpg://``."""
    parsed = make_url(url) if isinstance(url, str) else url
    if "+" not in parsed.drivername and parsed.drivername in _ASYNC_DRIVERS:
        parsed = parsed.set(drivername=_ASYNC_DRIVERS[parsed.drivername])
    return parsed


class _TxState:
    """The session bound to the current context plus its pending on_commit callbacks."""

    __slots__ = ("session", "callbacks", "finished", "commit_on_error", "lock")

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        # One list per open atomic() level; index 0 is the outer transaction.
        self.callbacks: list[list[Callable[[], Any]]] = [[]]
        self.finished = False
        self.commit_on_error = False
        # An AsyncSession must not be used concurrently. ORM calls sharing this
        # session (e.g. under asyncio.gather) take turns instead of failing.
        self.lock = asyncio.Lock()

    async def commit(self) -> None:
        self.finished = True
        try:
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            raise
        await _run_callbacks(self.callbacks[0])

    async def rollback(self) -> None:
        self.finished = True
        await self.session.rollback()


_current: ContextVar[Optional[_TxState]] = ContextVar("fastapi_mvt_session", default=None)
_default_database: Optional["Database"] = None


async def _run_callbacks(callbacks: list[Callable[[], Any]]) -> None:
    for callback in callbacks:
        try:
            result = callback()
            if inspect.isawaitable(result):
                await result
        except Exception:
            # The data is already committed; failing the request now would lie to the client.
            logger.exception("on_commit callback %r failed", callback)


class Database:
    """The project's database: engine, session factory and FastAPI integration.

    ::

        # myproject/db.py
        db = Database(settings.DATABASE_URL)

        # myproject/main.py
        db.init_app(app)
    """

    def __init__(self, url: str | URL, *, echo: bool = False, default: bool = True, **engine_options: Any) -> None:
        global _default_database
        self._url = to_async_url(url)
        self._echo = echo
        self._engine_options = engine_options
        self._engine: Optional[AsyncEngine] = None
        self._bind_override: Optional[AsyncConnection] = None
        if default or _default_database is None:
            _default_database = self

    # ── Engine ──────────────────────────────────────────────────────────────

    @property
    def url(self) -> URL:
        return self._url

    @property
    def is_sqlite(self) -> bool:
        return self._url.get_backend_name() == "sqlite"

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = self._create_engine()
        return self._engine

    def _create_engine(self) -> AsyncEngine:
        options = dict(self._engine_options)
        if not self.is_sqlite:
            # Check pooled connections before use, so a database restart or an idle
            # timeout doesn't turn the next requests into errors.
            options.setdefault("pool_pre_ping", True)
        if self.is_sqlite and self._url.database in (None, "", ":memory:"):
            # An in-memory database only exists inside one connection.
            options.setdefault("poolclass", StaticPool)
        engine = create_async_engine(self._url, echo=self._echo, **options)
        if self.is_sqlite:
            _configure_sqlite(engine, in_memory=self._url.database in (None, "", ":memory:"))
        return engine

    def configure(self, url: str | URL, **engine_options: Any) -> None:
        """Point this database at another URL (used by the test runner).

        Call ``await db.dispose()`` first if the old engine was already used.
        """
        self._url = to_async_url(url)
        if engine_options:
            self._engine_options = engine_options
        self._engine = None

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()

    # ── Sessions ────────────────────────────────────────────────────────────

    def new_session(self) -> AsyncSession:
        """Return a new, unmanaged ``AsyncSession``. You commit and close it."""
        if self._bind_override is not None:
            return AsyncSession(
                bind=self._bind_override,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
        return AsyncSession(bind=self.engine, expire_on_commit=False)

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        """An ``async_sessionmaker`` for libraries that want one (SQLAdmin, fastapi-users, ...).

        It always follows this database's current engine, including the test
        runner's per-test transaction.
        """
        return _DatabaseSessionMaker(self)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Open a session that commits on success and rolls back on error.

        ORM calls inside the block use this session automatically::

            async with db.session():
                author = await Author.objects.create(name="Ada")
                await Post.objects.create(title="Hi", author=author)
        """
        state = _TxState(self.new_session())
        token = _current.set(state)
        try:
            yield state.session
        except BaseException:
            if not state.finished:
                await state.rollback()
            raise
        else:
            if not state.finished:
                await state.commit()
        finally:
            _current.reset(token)
            await state.session.close()

    def run(self, func: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        """Run an async ORM function from synchronous code (Celery tasks, scripts).

        The whole call runs in one transaction::

            @celery_app.task
            def send_digest(user_id: int):
                db.run(build_digest, user_id)
        """

        async def main() -> Any:
            try:
                async with self.session():
                    return await func(*args, **kwargs)
            finally:
                await self.dispose()

        return asyncio.run(main())

    # ── Schema helpers (prototypes and tests; use migrations for real projects) ─

    async def create_all(self) -> None:
        from fastapi_mvt.db.models import Base

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        from fastapi_mvt.db.models import Base

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    # ── FastAPI ─────────────────────────────────────────────────────────────

    def init_app(self, app: Any, *, rollback_on_error_status: bool = True) -> None:
        """Give every HTTP request a transaction and map ORM errors to 404/409.

        The transaction commits before the response is sent and rolls back if the
        endpoint raises. With ``rollback_on_error_status=True`` (the default) it also
        rolls back when the response is an error (status >= 400), including a
        handled ``HTTPException``. Keep writes in a single request with
        :func:`commit_on_error`.
        """
        if getattr(app.state, "db", None) is self:
            return
        app.state.db = self
        app.add_middleware(
            DatabaseSessionMiddleware, database=self, rollback_on_error_status=rollback_on_error_status
        )
        install_exception_handlers(app)
        # Import every app's models.py so string relationships ("Post") resolve
        # even for models no router imports. A broken models.py fails loudly here.
        from fastapi_mvt.project import load_project_models

        load_project_models(required=False)


class _DatabaseSessionMaker(async_sessionmaker):  # type: ignore[type-arg]
    def __init__(self, database: Database) -> None:
        super().__init__(class_=AsyncSession, expire_on_commit=False)
        self._database = database

    def __call__(self, **local_kw: Any) -> AsyncSession:
        override = self._database._bind_override
        if override is not None:
            local_kw.setdefault("join_transaction_mode", "create_savepoint")
        return super().__call__(bind=override or self._database.engine, **local_kw)


def _configure_sqlite(engine: AsyncEngine, *, in_memory: bool) -> None:
    """Make SQLite behave: enforce foreign keys and support SAVEPOINT correctly."""

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        # Let SQLAlchemy, not the sqlite3 driver, decide when transactions start;
        # otherwise SAVEPOINTs (atomic(), test isolation) silently misbehave.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        if not in_memory:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _on_begin(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN")


def get_database() -> Database:
    if _default_database is None:
        raise SessionError(
            "No Database is configured. Create one in your project's db.py:\n\n"
            "    from fastapi_mvt.db import Database\n"
            "    db = Database(settings.DATABASE_URL)\n"
        )
    return _default_database


def current_session() -> AsyncSession:
    """Return the session of the current request / ``atomic()`` / ``db.session()`` block."""
    state = _current.get()
    if state is None or state.finished:
        raise SessionError(
            "There is no active database session here. Wrap the code in "
            "`async with atomic():` (or `async with db.session():`), or call it from a "
            "request handler of an app set up with `db.init_app(app)`."
        )
    return state.session


def in_transaction() -> bool:
    state = _current.get()
    return state is not None and not state.finished


@asynccontextmanager
async def session_scope(session: Optional[AsyncSession] = None) -> AsyncIterator[AsyncSession]:
    """Yield the session an ORM call should use (explicit, ambient, or a short-lived one)."""
    if session is not None:
        yield session
        return
    state = _current.get()
    if state is not None and not state.finished:
        async with state.lock:
            yield state.session
        return
    async with get_database().session() as new_session:
        yield new_session


@asynccontextmanager
async def atomic() -> AsyncIterator[AsyncSession]:
    """Run a block atomically, like Django's ``transaction.atomic``.

    Outside a transaction it opens one; inside one it creates a SAVEPOINT, so an
    error rolls back just this block::

        async with atomic():
            order = await Order.objects.create(...)
            await Stock.objects.filter(id=item_id).update(quantity=...)
    """
    state = _current.get()
    if state is None or state.finished:
        async with get_database().session() as session:
            yield session
        return

    state.callbacks.append([])
    try:
        async with state.session.begin_nested():
            yield state.session
    except BaseException:
        state.callbacks.pop()  # the savepoint rolled back, so its callbacks never run
        raise
    else:
        inner = state.callbacks.pop()
        state.callbacks[-1].extend(inner)


def commit_on_error() -> None:
    """Keep this request's writes even if the response turns out to be an error.

    By default an error response (status >= 400) rolls the request back. Call this
    before writing data that must survive it, e.g. a failed-login counter or an
    audit entry::

        commit_on_error()
        await LoginAttempt.objects.create(email=email, ok=False)
        raise HTTPException(401)

    Unhandled exceptions (500) still roll back.
    """
    state = _current.get()
    if state is None or state.finished:
        raise SessionError("commit_on_error() only works inside a request handled by db.init_app(app).")
    state.commit_on_error = True


_background: set[asyncio.Future] = set()


def on_commit(callback: Callable[[], Any]) -> None:
    """Run *callback* only after the current transaction commits successfully.

    Use it for side effects that must not happen if the data is rolled back:
    sending email, enqueueing a task, pushing a websocket event::

        post = await Post.objects.create(...)
        on_commit(lambda: notify_followers.delay(post.id))

    Async callbacks are awaited. With no transaction open, the callback runs now.
    """
    state = _current.get()
    if state is not None and not state.finished:
        state.callbacks[-1].append(callback)
        return
    result = callback()
    if inspect.isawaitable(result):
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # plain sync code: no loop to schedule on, so run it now
            asyncio.run(_await(lambda: result))
            return
        future = asyncio.ensure_future(result)
        _background.add(future)
        future.add_done_callback(_background.discard)


class DatabaseSessionMiddleware:
    """Pure-ASGI middleware giving each HTTP request one transaction.

    The commit happens right before the response headers are sent, so a client
    never receives a success response for data that failed to commit.
    """

    def __init__(self, app: Any, database: Database, rollback_on_error_status: bool = True) -> None:
        self.app = app
        self.database = database
        self.rollback_on_error_status = rollback_on_error_status

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            # Websockets live for minutes or hours; holding a transaction open that
            # long exhausts the pool. Their ORM calls use short autocommit sessions.
            await self.app(scope, receive, send)
            return

        state = _TxState(self.database.new_session())
        token = _current.set(state)

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start" and not state.finished:
                if message["status"] < 400 or state.commit_on_error or not self.rollback_on_error_status:
                    await state.commit()
                else:
                    await state.rollback()
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException:
            if not state.finished:
                await state.rollback()
            raise
        finally:
            _current.reset(token)
            await state.session.close()


# ── Sync code ─────────────────────────────────────────────────────────────────


async def _await(factory: Callable[[], Any]) -> Any:
    return await factory()


def _in_worker_thread() -> bool:
    from anyio import from_thread

    try:
        from_thread.run_sync(lambda: None)
        return True
    except RuntimeError:
        return False


def _run_awaitable(factory: Callable[[], Any]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("async_to_sync() is for synchronous code; in async code, use `await` directly.")
    if _in_worker_thread():
        # A FastAPI `def` endpoint or dependency: run on the app's event loop, inside
        # the request's transaction.
        from anyio import from_thread

        return from_thread.run(_await, factory)
    # A plain script or worker process: run in its own transaction.
    return get_database().run(_await, factory)


def async_to_sync(obj: Any) -> Any:
    """Use the ORM from synchronous code, like ``asgiref.sync.async_to_sync`` in Django.

    Pass an awaitable to get its result, or an async function to get a sync one::

        @router.get("/legacy")
        def legacy_endpoint():                       # a plain `def` endpoint
            return async_to_sync(Post.objects.filter(published=True))

        count = async_to_sync(Post.objects.count())
        save = async_to_sync(post.save); save()

    In a FastAPI ``def`` endpoint it joins the request's transaction; in a plain
    script it runs in a transaction of its own.
    """
    if inspect.iscoroutinefunction(obj):

        @functools.wraps(obj)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return _run_awaitable(lambda: obj(*args, **kwargs))

        return wrapper
    if inspect.isawaitable(obj) or hasattr(obj, "__await__"):
        return _run_awaitable(lambda: obj)
    raise TypeError(f"async_to_sync() takes an awaitable or an async function, not {type(obj).__name__}")
