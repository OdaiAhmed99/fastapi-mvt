from __future__ import annotations

import pytest

from fastapi_mvt.db import SessionError, atomic, current_session, in_transaction, on_commit
from tests.models import Author


async def test_atomic_commits_and_rolls_back(db):
    async with atomic():
        await Author.objects.create(name="kept")
    with pytest.raises(RuntimeError):
        async with atomic():
            await Author.objects.create(name="lost")
            raise RuntimeError("boom")
    assert await Author.objects.values_list("name", flat=True) == ["kept"]


async def test_nested_atomic_is_a_savepoint(db):
    async with atomic():
        await Author.objects.create(name="outer")
        with pytest.raises(ValueError):
            async with atomic():
                await Author.objects.create(name="inner")
                raise ValueError
        # Only the inner block was undone; the outer transaction continues.
        assert await Author.objects.filter(name="inner").count() == 0
        await Author.objects.create(name="after")
    assert sorted(await Author.objects.values_list("name", flat=True)) == ["after", "outer"]


async def test_on_commit_runs_only_after_a_successful_commit(db):
    events: list[str] = []

    async def notify() -> None:
        events.append("async")

    async with atomic():
        await Author.objects.create(name="a")
        on_commit(lambda: events.append("sync"))
        on_commit(notify)
        assert events == []  # not yet: still inside the transaction
    assert events == ["sync", "async"]

    events.clear()
    with pytest.raises(RuntimeError):
        async with atomic():
            on_commit(lambda: events.append("never"))
            raise RuntimeError
    assert events == []


async def test_on_commit_in_a_rolled_back_savepoint_is_discarded(db):
    events: list[str] = []
    async with atomic():
        on_commit(lambda: events.append("outer"))
        with pytest.raises(ValueError):
            async with atomic():
                on_commit(lambda: events.append("inner"))
                raise ValueError
    assert events == ["outer"]


async def test_on_commit_without_a_transaction_runs_immediately(db):
    events: list[str] = []
    on_commit(lambda: events.append("now"))
    assert events == ["now"]


async def test_failing_on_commit_callback_does_not_undo_the_commit(db, caplog):
    def broken() -> None:
        raise RuntimeError("email server down")

    async with atomic():
        await Author.objects.create(name="saved")
        on_commit(broken)
    assert await Author.objects.filter(name="saved").exists()
    assert "on_commit callback" in caplog.text


async def test_current_session_needs_a_transaction(db):
    assert not in_transaction()
    with pytest.raises(SessionError, match="atomic"):
        current_session()
    async with atomic() as session:
        assert in_transaction()
        assert current_session() is session


async def test_autocommit_outside_a_transaction(db):
    """Like Django: outside atomic() each ORM call commits on its own."""
    await Author.objects.create(name="one")
    with pytest.raises(RuntimeError):
        await Author.objects.create(name="two")
        raise RuntimeError
    assert await Author.objects.count() == 2


def test_db_run_from_sync_code(database):
    async def create_and_count(name: str) -> int:
        await Author.objects.create(name=name)
        return await Author.objects.filter(name=name).count()

    assert database.run(create_and_count, "from-sync") == 1

    async def cleanup() -> int:
        return await Author.objects.filter(name="from-sync").delete()

    assert database.run(cleanup) == 1


def test_on_commit_with_an_async_callback_from_sync_code():
    events: list[str] = []

    async def notify() -> None:
        events.append("ran")

    on_commit(notify)  # no transaction and no event loop: runs immediately
    assert events == ["ran"]
