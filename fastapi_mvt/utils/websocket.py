"""
fastapi_mvt.utils.websocket — group-aware WebSocket connection manager.

Import ``manager`` and call ``connect`` / ``disconnect`` in your route handlers.
Use ``send_to_group``, ``send_to_user``, or ``broadcast`` to push events.
For multi-process scaling, pass a channel backend to ``ConnectionManager``.
Full usage guide: see the online docs.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, Set

from fastapi import WebSocket


# ---------------------------------------------------------------------------
# Channel backends
# ---------------------------------------------------------------------------

ChannelHandler = Callable[[str, Dict[str, Any]], Awaitable[None]]


class BaseChannelBackend(ABC):
    """Abstract pub/sub backend for distributed WebSocket events."""

    @abstractmethod
    async def publish(self, channel: str, message: Dict[str, Any]) -> None:
        """Publish a message to a backend channel."""
        raise NotImplementedError

    @abstractmethod
    async def subscribe(
        self, channels: Iterable[str], handler: ChannelHandler
    ) -> None:
        """Subscribe to backend channels and pass messages to handler."""
        raise NotImplementedError


class InMemoryChannelBackend(BaseChannelBackend):
    """No-op backend used when no distributed channel layer is configured."""

    async def publish(self, channel: str, message: Dict[str, Any]) -> None:
        return None

    async def subscribe(
        self, channels: Iterable[str], handler: ChannelHandler
    ) -> None:
        return None


class RedisChannelBackend(BaseChannelBackend):
    """Redis Pub/Sub backend.

    The redis client can be an instance from ``redis.asyncio``.
    """

    def __init__(self, redis_client: Any) -> None:
        self.redis = redis_client

    async def publish(self, channel: str, message: Dict[str, Any]) -> None:
        await self.redis.publish(channel, json.dumps(message))

    async def subscribe(
        self, channels: Iterable[str], handler: ChannelHandler
    ) -> None:
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(*channels)
        async for raw_message in pubsub.listen():
            if raw_message.get("type") != "message":
                continue
            channel = raw_message.get("channel")
            data = raw_message.get("data")
            if isinstance(channel, bytes):
                channel = channel.decode()
            if isinstance(data, bytes):
                data = data.decode()
            await handler(str(channel), json.loads(data))


# ---------------------------------------------------------------------------
# Internal metadata attached to each connection
# ---------------------------------------------------------------------------

@dataclass
class _ConnectionMeta:
    user_id: Optional[str] = None
    groups: Set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Connection Manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    """
    Async WebSocket manager with group and per-user fan-out.

    All send / broadcast methods are coroutines — always ``await`` them.
    Import the module-level ``manager`` singleton rather than instantiating
    this class directly.
    """

    GROUP_CHANNEL = "websocket:group"
    USER_CHANNEL = "websocket:user"
    BROADCAST_CHANNEL = "websocket:broadcast"

    def __init__(self, backend: Optional[BaseChannelBackend] = None) -> None:
        self._groups: Dict[str, Set[WebSocket]] = {}            # group  → connections
        self._user_connections: Dict[str, Set[WebSocket]] = {}   # user   → connections
        self._connection_meta: Dict[WebSocket, _ConnectionMeta] = {}  # ws → meta
        self.backend = backend or InMemoryChannelBackend()
        self._backend_sender_id = str(uuid.uuid4())

    # ── Connection lifecycle ──────────────────────────────────────────────────

    async def connect(
        self,
        websocket: WebSocket,
        *,
        group: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        """
        Accept the WebSocket handshake and register the connection.

        Parameters
        ----------
        websocket:
            The incoming ``WebSocket`` instance from the route handler.
        group:
            Optional group name to add this connection to immediately.
            Additional groups can be joined later via ``add_to_group``.
        user_id:
            Optional opaque user identifier (e.g. ``str(user.id)``).
            Required for ``send_to_user`` to work for this connection.
        """
        await websocket.accept()
        meta = _ConnectionMeta(user_id=user_id)
        self._connection_meta[websocket] = meta
        if user_id is not None:
            self._user_connections.setdefault(user_id, set()).add(websocket)
        if group is not None:
            await self.add_to_group(websocket, group)

    def disconnect(self, websocket: WebSocket) -> None:
        """
        Remove a connection from all groups and user tracking.

        Call this inside the ``except WebSocketDisconnect`` block of your
        route handler — no need to specify the group or user_id.
        """
        meta = self._connection_meta.pop(websocket, None)
        if meta is None:
            return
        for group in list(meta.groups):
            group_set = self._groups.get(group)
            if group_set:
                group_set.discard(websocket)
                if not group_set:
                    del self._groups[group]
        if meta.user_id is not None:
            user_set = self._user_connections.get(meta.user_id)
            if user_set:
                user_set.discard(websocket)
                if not user_set:
                    del self._user_connections[meta.user_id]

    # ── Group membership ─────────────────────────────────────────────────────

    async def add_to_group(self, websocket: WebSocket, group: str) -> None:
        """Add an already-connected WebSocket to an additional group."""
        self._groups.setdefault(group, set()).add(websocket)
        meta = self._connection_meta.get(websocket)
        if meta is not None:
            meta.groups.add(group)

    async def remove_from_group(self, websocket: WebSocket, group: str) -> None:
        """Remove a WebSocket from a group without closing the connection."""
        group_set = self._groups.get(group)
        if group_set:
            group_set.discard(websocket)
            if not group_set:
                del self._groups[group]
        meta = self._connection_meta.get(websocket)
        if meta is not None:
            meta.groups.discard(group)

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _build_payload(event: str, data: Any) -> dict:
        return {"type": event, "data": data}

    async def _send_event(
        self, websocket: WebSocket, event: str, data: Any
    ) -> None:
        """Send one event to one connection, silently dropping broken sockets."""
        try:
            await websocket.send_json(self._build_payload(event, data))
        except Exception:
            pass  # already closed; disconnect() will clean up the meta entry

    async def _fan_out(
        self, connections: Set[WebSocket], event: str, data: Any
    ) -> None:
        """Send an event to a snapshot of connections concurrently."""
        if not connections:
            return
        await asyncio.gather(
            *(self._send_event(ws, event, data) for ws in set(connections)),
            return_exceptions=True,
        )

    # ── Send to group ─────────────────────────────────────────────────────────

    async def _handle_backend_message(
        self, channel: str, message: Dict[str, Any]
    ) -> None:
        """Deliver a backend message to this worker's local connections."""
        if message.get("sender_id") == self._backend_sender_id:
            return
        event = message.get("event")
        data = message.get("data")
        if not event:
            return

        if channel == self.GROUP_CHANNEL:
            group = message.get("group")
            if group:
                await self._fan_out(self._groups.get(group, set()), event, data)
        elif channel == self.USER_CHANNEL:
            user_id = message.get("user_id")
            if user_id:
                await self._fan_out(self._user_connections.get(user_id, set()), event, data)
        elif channel == self.BROADCAST_CHANNEL:
            all_connections: Set[WebSocket] = set(self._connection_meta.keys())
            await self._fan_out(all_connections, event, data)

    async def listen_for_backend_events(self) -> None:
        """Subscribe to backend events and deliver them to local connections."""
        await self.backend.subscribe(
            (self.GROUP_CHANNEL, self.USER_CHANNEL, self.BROADCAST_CHANNEL),
            self._handle_backend_message,
        )

    async def send_to_group(
        self, *, group: str, event: str, data: Any = None
    ) -> None:
        """
        Send a JSON event to every connection in *group*.

        Payload sent:  ``{"type": event, "data": data}``

        Parameters
        ----------
        group:
            Group name, e.g. ``"project_42_alerts"``.
        event:
            Event type string, e.g. ``"new_alert"``.
        data:
            Any JSON-serialisable value.

        Usage from a signal / service
        ------------------------------
            await manager.send_to_group(
                group=f"project_{project_id}_alerts",
                event="new_alert",
                data={"id": instance.id, "message": instance.message},
            )
        """
        await self._fan_out(self._groups.get(group, set()), event, data)
        await self.backend.publish(
            self.GROUP_CHANNEL,
            {
                "sender_id": self._backend_sender_id,
                "group": group,
                "event": event,
                "data": data,
            },
        )

    # ── Send to user ──────────────────────────────────────────────────────────

    async def send_to_user(
        self, *, user_id: str, event: str, data: Any = None
    ) -> None:
        """
        Send a JSON event to all open connections belonging to *user_id*.

        Reaches every browser tab / device the user has open simultaneously.

        Parameters
        ----------
        user_id:
            The identifier passed to ``connect(user_id=...)``.
        event:
            Event type string.
        data:
            Any JSON-serialisable value.

        Usage
        -----
            await manager.send_to_user(
                user_id=str(user.id),
                event="notification",
                data={"message": "Your export is ready."},
            )
        """
        await self._fan_out(self._user_connections.get(user_id, set()), event, data)
        await self.backend.publish(
            self.USER_CHANNEL,
            {
                "sender_id": self._backend_sender_id,
                "user_id": user_id,
                "event": event,
                "data": data,
            },
        )


    # ── Broadcast ─────────────────────────────────────────────────────────────

    async def broadcast(self, *, event: str, data: Any = None) -> None:
        """
        Send a JSON event to **every** connected client across all groups.

        Use for system-wide announcements (maintenance alerts, global updates).

        Parameters
        ----------
        event:
            Event type string.
        data:
            Any JSON-serialisable value.

        Usage
        -----
            await manager.broadcast(
                event="maintenance",
                data={"message": "Server restart in 5 minutes."},
            )
        """
        all_connections: Set[WebSocket] = set(self._connection_meta.keys())
        await self._fan_out(all_connections, event, data)
        await self.backend.publish(
            self.BROADCAST_CHANNEL,
            {
                "sender_id": self._backend_sender_id,
                "event": event,
                "data": data,
            },
        )

    # ── Stats / introspection ─────────────────────────────────────────────────

    @property
    def connection_count(self) -> int:
        """Total number of open connections across all groups."""
        return len(self._connection_meta)

    def group_count(self, group: str) -> int:
        """Number of open connections in *group*."""
        return len(self._groups.get(group, set()))

    def active_groups(self) -> list[str]:
        """Return names of all groups that have at least one connection."""
        return [g for g, conns in self._groups.items() if conns]

    def user_connection_count(self, user_id: str) -> int:
        """Number of open connections for a given *user_id*."""
        return len(self._user_connections.get(user_id, set()))


__all__ = [
    "BaseChannelBackend",
    "ConnectionManager",
    "InMemoryChannelBackend",
    "RedisChannelBackend",
]
