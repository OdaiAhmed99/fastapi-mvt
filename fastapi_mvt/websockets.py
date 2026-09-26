"""WebSocket fan-out to groups and users, optionally across processes via Redis.

::

    manager = ConnectionManager()                      # or ConnectionManager(RedisChannelBackend(redis))

    @router.websocket("/events/{event_id}/live")
    async def live(websocket: WebSocket, event_id: int):
        await manager.connect(websocket, group=f"event:{event_id}")
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            manager.disconnect(websocket)

    # anywhere, e.g. after a commit:
    on_commit(lambda: manager.send_to_group(group=f"event:{event_id}", event="liked", data={...}))

With a Redis backend call ``await manager.start()`` at startup and
``await manager.stop()`` at shutdown (e.g. in the app lifespan).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Optional

from starlette.websockets import WebSocket

logger = logging.getLogger("fastapi_mvt.websockets")
Handler = Callable[[str, dict[str, Any]], Awaitable[None]]


class ChannelBackend(ABC):
    @abstractmethod
    async def publish(self, channel: str, message: dict[str, Any]) -> None: ...

    @abstractmethod
    async def subscribe(self, channels: Iterable[str], handler: Handler) -> None:
        """Deliver messages to *handler* until cancelled."""


class InMemoryChannelBackend(ChannelBackend):
    """Single process: nothing to publish, since local delivery already happened."""

    async def publish(self, channel: str, message: dict[str, Any]) -> None:
        return None

    async def subscribe(self, channels: Iterable[str], handler: Handler) -> None:
        await asyncio.Event().wait()


class RedisChannelBackend(ChannelBackend):
    """Redis pub/sub (``pip install "fastapi-mvt[redis]"``); pass a ``redis.asyncio`` client."""

    def __init__(self, redis: Any) -> None:
        self.redis = redis

    async def publish(self, channel: str, message: dict[str, Any]) -> None:
        await self.redis.publish(channel, json.dumps(message, default=str))

    async def subscribe(self, channels: Iterable[str], handler: Handler) -> None:
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(*channels)
        try:
            async for raw in pubsub.listen():
                if raw.get("type") != "message":
                    continue
                channel = raw["channel"].decode() if isinstance(raw["channel"], bytes) else raw["channel"]
                data = raw["data"].decode() if isinstance(raw["data"], bytes) else raw["data"]
                try:
                    await handler(str(channel), json.loads(data))
                except Exception:
                    logger.exception("WebSocket backend handler failed")
        finally:
            await pubsub.aclose()


@dataclass
class _Meta:
    user_id: Optional[str] = None
    groups: set[str] = field(default_factory=set)


class ConnectionManager:
    GROUP = "fastapi_mvt:ws:group"
    USER = "fastapi_mvt:ws:user"
    BROADCAST = "fastapi_mvt:ws:broadcast"

    def __init__(self, backend: Optional[ChannelBackend] = None) -> None:
        self.backend = backend or InMemoryChannelBackend()
        self._groups: dict[str, set[WebSocket]] = {}
        self._users: dict[str, set[WebSocket]] = {}
        self._meta: dict[WebSocket, _Meta] = {}
        self._id = uuid.uuid4().hex
        self._listener: Optional[asyncio.Task[None]] = None

    # Lifecycle ────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start receiving messages published by other processes."""
        if self._listener is None:
            self._listener = asyncio.create_task(
                self.backend.subscribe((self.GROUP, self.USER, self.BROADCAST), self._on_backend_message)
            )

    async def stop(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener
            self._listener = None

    # Connections ──────────────────────────────────────────────────────────────

    async def connect(self, websocket: WebSocket, *, group: Optional[str] = None, user_id: Any = None) -> None:
        await websocket.accept()
        meta = _Meta(user_id=str(user_id) if user_id is not None else None)
        self._meta[websocket] = meta
        if meta.user_id is not None:
            self._users.setdefault(meta.user_id, set()).add(websocket)
        if group is not None:
            self.add_to_group(websocket, group)

    def disconnect(self, websocket: WebSocket) -> None:
        meta = self._meta.pop(websocket, None)
        if meta is None:
            return
        for group in meta.groups:
            members = self._groups.get(group)
            if members is not None:
                members.discard(websocket)
                if not members:
                    del self._groups[group]
        if meta.user_id is not None:
            sockets = self._users.get(meta.user_id)
            if sockets is not None:
                sockets.discard(websocket)
                if not sockets:
                    del self._users[meta.user_id]

    def add_to_group(self, websocket: WebSocket, group: str) -> None:
        self._groups.setdefault(group, set()).add(websocket)
        if websocket in self._meta:
            self._meta[websocket].groups.add(group)

    def remove_from_group(self, websocket: WebSocket, group: str) -> None:
        members = self._groups.get(group)
        if members is not None:
            members.discard(websocket)
            if not members:
                del self._groups[group]
        if websocket in self._meta:
            self._meta[websocket].groups.discard(group)

    # Sending ──────────────────────────────────────────────────────────────────

    async def _send(self, websocket: WebSocket, event: str, data: Any) -> None:
        try:
            await websocket.send_json({"type": event, "data": data})
        except Exception:
            # The socket is gone; forget it now instead of failing on every future send.
            self.disconnect(websocket)

    async def _fan_out(self, sockets: Iterable[WebSocket], event: str, data: Any) -> None:
        targets = list(sockets)
        if targets:
            await asyncio.gather(*(self._send(ws, event, data) for ws in targets))

    async def send_to_group(self, *, group: str, event: str, data: Any = None) -> None:
        await self._fan_out(self._groups.get(group, ()), event, data)
        await self.backend.publish(self.GROUP, {"origin": self._id, "group": group, "event": event, "data": data})

    async def send_to_user(self, *, user_id: Any, event: str, data: Any = None) -> None:
        key = str(user_id)
        await self._fan_out(self._users.get(key, ()), event, data)
        await self.backend.publish(self.USER, {"origin": self._id, "user_id": key, "event": event, "data": data})

    async def broadcast(self, *, event: str, data: Any = None) -> None:
        await self._fan_out(self._meta.keys(), event, data)
        await self.backend.publish(self.BROADCAST, {"origin": self._id, "event": event, "data": data})

    async def _on_backend_message(self, channel: str, message: dict[str, Any]) -> None:
        if message.get("origin") == self._id or not message.get("event"):
            return
        if channel == self.GROUP:
            sockets: Iterable[WebSocket] = self._groups.get(message.get("group", ""), ())
        elif channel == self.USER:
            sockets = self._users.get(message.get("user_id", ""), ())
        else:
            sockets = self._meta.keys()
        await self._fan_out(sockets, message["event"], message.get("data"))

    # Introspection ────────────────────────────────────────────────────────────

    @property
    def connection_count(self) -> int:
        return len(self._meta)

    def group_size(self, group: str) -> int:
        return len(self._groups.get(group, ()))
