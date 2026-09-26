from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient

from fastapi_mvt.auth import Auth
from fastapi_mvt.websockets import ConnectionManager
from tests.models import Account

SECRET = "test-secret-key-that-is-long-enough-0123456789"


def test_admin_requires_auth(database):
    pytest.importorskip("sqladmin")
    from fastapi_mvt.admin import setup_admin

    with pytest.raises(ValueError, match="auth="):
        setup_admin(FastAPI(), database)


async def test_admin_login_only_for_superusers(db):
    pytest.importorskip("sqladmin")
    from fastapi_mvt.admin import setup_admin

    auth = Auth(Account, secret_key=SECRET)
    app = FastAPI()
    db.init_app(app)
    setup_admin(app, db, auth=auth, models=[Account])

    regular = Account(email="user@example.com")
    await regular.set_password("password-1")
    admin = Account(email="admin@example.com", is_superuser=True)
    await admin.set_password("password-2")
    await Account.objects.bulk_create([regular, admin])

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/admin/")).status_code in (302, 303, 307)
        r = await c.post("/admin/login", data={"username": "user@example.com", "password": "password-1"})
        assert r.status_code == 400
        r = await c.post("/admin/login", data={"username": "admin@example.com", "password": "password-2"})
        assert r.status_code in (302, 303)
        listing = await c.get("/admin/account/list")
        assert listing.status_code == 200
        assert "admin@example.com" in listing.text
        assert "pbkdf2_sha256" not in listing.text  # private columns are hidden


def test_websocket_groups_and_dead_sockets():
    manager = ConnectionManager()
    app = FastAPI()

    @app.websocket("/ws/{room}")
    async def ws(websocket: WebSocket, room: str):
        await manager.connect(websocket, group=room, user_id="u1")
        try:
            while True:
                text = await websocket.receive_text()
                await manager.send_to_group(group=room, event="message", data=text)
        except WebSocketDisconnect:
            manager.disconnect(websocket)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/a") as first, client.websocket_connect("/ws/a") as second:
            first.send_text("hi")
            assert first.receive_json() == {"type": "message", "data": "hi"}
            assert second.receive_json() == {"type": "message", "data": "hi"}
            assert manager.group_size("a") == 2
    assert manager.connection_count == 0


async def test_send_failure_forgets_the_socket():
    class Broken:
        async def accept(self) -> None: ...

        async def send_json(self, data) -> None:
            raise RuntimeError("closed")

    manager = ConnectionManager()
    await manager.connect(Broken(), group="g")  # type: ignore[arg-type]
    await manager.send_to_group(group="g", event="x")
    assert manager.connection_count == 0 and manager.group_size("g") == 0
    await manager.start()
    await manager.stop()
    await asyncio.sleep(0)
