"""The request lifecycle: one transaction per request, errors mapped to status codes."""

from __future__ import annotations

import httpx
import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from fastapi_mvt.db import Page, PageParams, model_schema, on_commit
from tests.models import Author


class AuthorRead(model_schema(Author)):
    pass


AuthorCreate = model_schema(Author, "create")


def build_app(database) -> FastAPI:
    app = FastAPI()
    database.init_app(app)
    events: list[str] = []
    app.state.events = events

    @app.post("/authors", response_model=AuthorRead, status_code=201)
    async def create(data: AuthorCreate):  # type: ignore[valid-type]
        author = await Author.objects.create(**data.model_dump())
        on_commit(lambda: events.append(f"created {author.name}"))
        return author

    @app.post("/authors/fail")
    async def create_then_fail():
        await Author.objects.create(name="ghost")
        raise HTTPException(403, "no")

    @app.post("/authors/crash")
    async def create_then_crash():
        await Author.objects.create(name="ghost")
        raise RuntimeError("bug")

    @app.post("/authors/error-response")
    async def create_then_error_response():
        await Author.objects.create(name="ghost")
        return JSONResponse({"detail": "bad"}, status_code=400)

    @app.get("/authors/{author_id}", response_model=AuthorRead)
    async def get(author_id: int):
        return await Author.objects.get(id=author_id)

    @app.get("/authors", response_model=Page[AuthorRead])
    async def list_authors(params: PageParams = Depends_page()):
        return await Author.objects.order_by("id").paginate(params)

    @app.post("/authors/background")
    async def with_background(tasks: BackgroundTasks):
        async def later() -> None:
            await Author.objects.create(name="from background")

        tasks.add_task(later)
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await Author.objects.create(name="from websocket")
        await websocket.send_json({"count": await Author.objects.count()})
        await websocket.close()

    return app


def Depends_page():
    from fastapi import Depends

    return Depends(PageParams)


@pytest.fixture
async def client(db):
    app = build_app(db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        c.app = app  # type: ignore[attr-defined]
        yield c


async def test_success_commits_and_runs_on_commit(client):
    response = await client.post("/authors", json={"name": "Ada", "email": "ada@example.com"})
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "Ada"
    assert "created_at" in response.json()
    assert client.app.state.events == ["created Ada"]
    assert await Author.objects.filter(name="Ada").exists()


@pytest.mark.parametrize("path", ["/authors/fail", "/authors/error-response"])
async def test_error_responses_roll_back(client, path):
    response = await client.post(path)
    assert response.status_code in (400, 403)
    assert not await Author.objects.filter(name="ghost").exists()


async def test_unhandled_exception_rolls_back(db):
    app = build_app(db)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
    ) as c:
        assert (await c.post("/authors/crash")).status_code == 500
    assert not await Author.objects.filter(name="ghost").exists()


async def test_does_not_exist_is_404_and_integrity_error_is_409(client):
    response = await client.get("/authors/999999")
    assert response.status_code == 404
    assert response.json() == {"detail": "Author not found."}

    assert (await client.post("/authors", json={"name": "A", "email": "same@example.com"})).status_code == 201
    response = await client.post("/authors", json={"name": "B", "email": "same@example.com"})
    assert response.status_code == 409
    assert "same@example.com" not in response.text  # driver details are not leaked


async def test_page_response_model_serialises_orm_objects(client):
    for i in range(3):
        await Author.objects.create(name=f"a{i}")
    response = await client.get("/authors", params={"page": 2, "size": 2})
    body = response.json()
    assert response.status_code == 200, body
    assert body["total"] == 3 and body["pages"] == 2 and body["has_previous"] is True
    assert [a["name"] for a in body["items"]] == ["a2"]
    assert (await client.get("/authors", params={"size": 1000})).status_code == 422


async def test_request_validation_uses_column_constraints(client):
    response = await client.post("/authors", json={"name": "x" * 101})
    assert response.status_code == 422
    assert "100" in response.text


async def test_background_tasks_run_in_their_own_transaction(client):
    response = await client.post("/authors/background")
    assert response.status_code == 200
    assert await Author.objects.filter(name="from background").exists()


def test_websockets_use_short_transactions(database):
    app = build_app(database)
    with TestClient(app) as tc, tc.websocket_connect("/ws") as ws:
        assert ws.receive_json()["count"] >= 1

    async def cleanup() -> None:
        await Author.objects.filter(name="from websocket").delete()
        await database.dispose()

    import asyncio

    asyncio.run(cleanup())


async def test_sync_endpoints_use_the_request_transaction(db):
    from fastapi_mvt.db import async_to_sync

    app = FastAPI()
    db.init_app(app)

    @app.post("/sync/{name}")
    def create_sync(name: str):  # a plain `def` endpoint, run in a worker thread
        author = async_to_sync(Author.objects.create(name=name))
        count = async_to_sync(Author.objects.filter(name=name).count())
        if name == "fail":
            raise HTTPException(400, "rolled back")
        return {"id": author.id, "count": count}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        response = await c.post("/sync/Ada")
        assert response.status_code == 200 and response.json()["count"] == 1
        assert (await c.post("/sync/fail")).status_code == 400
    assert await Author.objects.filter(name="Ada").exists()
    assert not await Author.objects.filter(name="fail").exists()  # same transaction, rolled back


def test_async_to_sync_in_plain_scripts(database):
    from fastapi_mvt.db import async_to_sync

    author = async_to_sync(Author.objects.create(name="script"))
    assert async_to_sync(Author.objects.filter(id=author.id).count()) == 1
    delete = async_to_sync(Author.objects.filter(name="script").delete)
    assert delete() == 1


async def test_commit_on_error_keeps_writes(db):
    from fastapi_mvt.db import commit_on_error

    app = FastAPI()
    db.init_app(app)

    @app.post("/audit")
    async def audit():
        commit_on_error()
        await Author.objects.create(name="audit entry")
        raise HTTPException(403, "denied, but logged")

    @app.post("/crash")
    async def crash():
        commit_on_error()
        await Author.objects.create(name="never")
        raise RuntimeError("bug")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
    ) as c:
        assert (await c.post("/audit")).status_code == 403
        assert (await c.post("/crash")).status_code == 500
    assert await Author.objects.filter(name="audit entry").exists()
    assert not await Author.objects.filter(name="never").exists()  # unhandled errors still roll back


async def test_rollback_on_error_status_can_be_disabled(db):
    app = FastAPI()
    db.init_app(app, rollback_on_error_status=False)

    @app.post("/partial")
    async def partial():
        await Author.objects.create(name="kept")
        return JSONResponse({"detail": "validation failed"}, status_code=422)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/partial")).status_code == 422
    assert await Author.objects.filter(name="kept").exists()


async def test_concurrent_orm_calls_in_one_request(db):
    """asyncio.gather on the request's shared session must not fail (queries take turns)."""
    import asyncio

    app = FastAPI()
    db.init_app(app)

    @app.get("/both")
    async def both():
        authors, count, exists = await asyncio.gather(
            Author.objects.all(), Author.objects.count(), Author.objects.filter(name="a1").exists()
        )
        return {"authors": len(authors), "count": count, "exists": exists}

    for i in range(5):
        await Author.objects.create(name=f"a{i}")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/both")).json() == {"authors": 5, "count": 5, "exists": True}


async def test_update_schema_rejects_null_for_required_fields(client):
    from tests.models import Post

    PostUpdate = model_schema(Post, "update")
    assert PostUpdate().model_dump(exclude_unset=True) == {}           # fields may be left out
    assert PostUpdate(body=None).body is None                          # nullable column: null is fine
    with pytest.raises(Exception, match="title"):
        PostUpdate(title=None)                                         # NOT NULL column: null is a 422
