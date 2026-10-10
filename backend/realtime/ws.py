"""Room connection hub + per-connection writer.

Two clean directions per connection:
  * OUT (server -> many clients): agent/steering threads call
    :meth:`RoomHub.send_event` -> bus.publish -> each :class:`Connection`
    enqueues the JSON string -> its asyncio writer task ``send_text``s it.
    Cross-thread safe via ``loop.call_soon_threadsafe``.
  * IN (client -> server): the endpoint reads ``ws.receive_text()`` and
    dispatches RPC, replying straight on the socket (async context).

This decoupling is what makes steering non-blocking: the agent thread and the
event loop never share a lock; they hand messages through thread-safe queues.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import defaultdict
from typing import Dict, List, Optional, Set

from shared.protocols.python.protocols import Envelope, MessageKind, User
from .bus import PubSubBus


def topic_for(room_id: str) -> str:
    return f"room:{room_id}"


class Connection:
    """One live WebSocket, bound to a room and a user."""

    def __init__(self, ws, room_id: str, user: User, hub: "RoomHub"):
        self.ws = ws
        self.room_id = room_id
        self.user = user
        self.hub = hub
        self._out: asyncio.Queue = asyncio.Queue()

    # -- outbound -------------------------------------------------------
    def _enqueue(self, data: Optional[str]) -> None:
        self._out.put_nowait(data)

    def send_threadsafe(self, data: str) -> None:
        self.hub.loop.call_soon_threadsafe(self._enqueue, data)

    def close_writer(self) -> None:
        self.hub.loop.call_soon_threadsafe(self._enqueue, None)  # sentinel

    async def write_loop(self) -> None:
        while True:
            data = await self._out.get()
            if data is None:
                break
            try:
                await self.ws.send_text(data)
            except Exception:
                break  # socket gone; let the read loop tear us down

    # -- inbound helper -------------------------------------------------
    async def send_response(self, response_env: Envelope) -> None:
        await self.ws.send_text(json.dumps(response_env.to_dict()))


class RoomHub:
    def __init__(self, loop: asyncio.AbstractEventLoop, bus: Optional[PubSubBus] = None):
        self.loop = loop
        self.bus = bus or PubSubBus()
        self._rooms: Dict[str, Set[Connection]] = defaultdict(set)
        self._seq: Dict[str, int] = defaultdict(int)
        self._mutex = threading.Lock()

    def connect(self, room_id: str, ws, user: User) -> Connection:
        conn = Connection(ws, room_id, user, self)
        # subscribe this connection to its room topic: bus.publish delivers here
        self.bus.subscribe(topic_for(room_id), conn.send_threadsafe)
        with self._mutex:
            self._rooms[room_id].add(conn)
        return conn

    def disconnect(self, conn: Connection) -> None:
        self.bus.unsubscribe(topic_for(conn.room_id), conn.send_threadsafe)
        with self._mutex:
            self._rooms[conn.room_id].discard(conn)
        conn.close_writer()

    # -- fan-out --------------------------------------------------------
    def send_event(self, room_id: str, kind: MessageKind,
                   payload: Optional[dict] = None, sender: str = "agent") -> Envelope:
        with self._mutex:
            self._seq[room_id] += 1
            seq = self._seq[room_id]
        env = Envelope.event(kind, room_id, payload or {}, sender=sender, seq=seq)
        data = json.dumps(env.to_dict())
        self.bus.publish(topic_for(room_id), data)
        return env

    def send_raw(self, room_id: str, data: str) -> None:
        self.bus.publish(topic_for(room_id), data)

    def send_envelope(self, room_id: str, env: Envelope) -> None:
        self.send_raw(room_id, json.dumps(env.to_dict()))

    # -- introspection --------------------------------------------------
    def room_size(self, room_id: str) -> int:
        with self._mutex:
            return len(self._rooms.get(room_id, ()))

    def rooms(self) -> List[str]:
        with self._mutex:
            return list(self._rooms.keys())
