"""Multiplayer AI Agent — shared wire protocols.

This module is the SINGLE SOURCE OF TRUTH for the on-wire contract shared by
the Python backend and the Swift/SwiftUI frontend. Every WebSocket frame and
RPC request/response travels as an :class:`Envelope`.

Mirrors:
  * JSON Schema  ->  shared/protocols/envelope.schema.json
  * Swift        ->  shared/protocols/swift/Protocols.swift

Keep all three in lockstep. When you change a field here, change it in the
other two and in the human spec (shared/protocols/protocol.md).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


PROTOCOL_VERSION = 1


def _new_id() -> str:
    return uuid.uuid4().hex


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class MessageKind(str, Enum):
    """The `type` discriminator on every envelope.

    Split into three families:
      * ``rpc.*``      request/response calls (client <-> server)
      * ``agent.*``    execution events streamed to a room (server -> clients)
      * ``state.*``    state-graph + snapshot events (server -> clients)
      * ``room.*``     presence / membership (server -> clients)
      * ``steering.*`` steering status + lock changes (server -> clients)
    """

    # RPC
    RPC = "rpc"
    RPC_RESPONSE = "rpc.response"

    # Agent execution events
    AGENT_STARTED = "agent.started"
    AGENT_THOUGHT = "agent.thought"
    AGENT_TOOL_CALL = "agent.tool_call"
    AGENT_TOOL_RESULT = "agent.tool_result"
    AGENT_STEP = "agent.step"
    AGENT_DONE = "agent.done"
    AGENT_ERROR = "agent.error"

    # State graph events
    STATE_NODE_ADDED = "state.node_added"
    STATE_SNAPSHOT = "state.snapshot"
    STATE_BRANCH = "state.branch"
    STATE_MERGE = "state.merge"

    # Room / presence
    ROOM_PRESENCE = "room.presence"

    # Steering
    STEERING_STATUS = "steering.status"
    STEERING_LOCK_CHANGED = "steering.lock_changed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_UPDATED = "approval.updated"


class RunState(str, Enum):
    """Lifecycle of an agent run inside a room."""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    FINISHED = "finished"
    FAILED = "failed"


class SteeringAction(str, Enum):
    """Steering verbs a holder of the lock may apply."""

    PAUSE = "pause"
    RESUME = "resume"
    INTERRUPT = "interrupt"
    INJECT = "inject"


class UserRole(str, Enum):
    """Coarse role label — purely informational for the UI."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    STEERER = "steerer"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass
class User:
    id: str
    name: str
    role: UserRole = UserRole.OPERATOR


@dataclass
class Presence:
    """A member currently attached to a room."""

    user: User
    connected_at: float = field(default_factory=_now)
    is_host: bool = False


@dataclass
class ToolCall:
    name: str
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    name: str
    ok: bool
    result: Any = None
    error: Optional[str] = None
    duration_ms: int = 0


@dataclass
class MemoryEntry:
    """One line of the agent's working memory at a given node."""

    kind: str  # "system" | "user" | "thought" | "tool" | "injection"
    text: str


@dataclass
class Snapshot:
    """An immutable, content-addressed snapshot of agent state at a graph node.

    This is what time-travel scrubbing inspects. It is a *point-in-time copy*
    — branching/merging operates on nodes, and each node pins exactly one of
    these by its ``snapshot_id``.
    """

    snapshot_id: str = field(default_factory=_new_id)
    node_id: str = ""
    step_index: int = 0
    run_state: RunState = RunState.RUNNING
    memory: List[MemoryEntry] = field(default_factory=list)
    scratch: Dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""  # filled by hashing the canonical form
    created_at: float = field(default_factory=_now)


@dataclass
class GraphNode:
    """A node in the execution state graph.

    ``parents`` is a list (>=1): a single parent is a linear step, multiple
    parents is a merge. Branching = a new node reusing the same parent(s).
    """

    node_id: str = field(default_factory=_new_id)
    room_id: str = ""
    label: str = ""
    parents: List[str] = field(default_factory=list)
    step_index: int = 0
    kind: str = "step"  # "step" | "branch" | "merge" | "root"
    snapshot_id: str = ""
    created_at: float = field(default_factory=_now)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphEdge:
    src: str  # parent
    dst: str  # child
    weight: float = 1.0


@dataclass
class SteeringLock:
    """The single mutable steering token for a room.

    Exactly one holder at a time. Hand-off is an atomic compare-and-swap on
    ``token`` so two clients can never both hold steering rights.
    """

    room_id: str
    holder_id: Optional[str] = None
    holder_name: Optional[str] = None
    token: str = field(default_factory=_new_id)
    acquired_at: float = field(default_factory=_now)


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------
@dataclass
class Envelope:
    """The wire format for every message.

    ``type`` discriminates; ``payload`` is a flat dict whose shape depends on
    ``type``. Keeping the payload a plain dict (not a nested dataclass) makes
    the JSON Schema and the Swift ``Codable`` mirror trivial to keep in sync.
    """

    type: MessageKind
    v: int = PROTOCOL_VERSION
    id: str = field(default_factory=_new_id)
    room_id: str = ""
    seq: int = 0
    ts: float = field(default_factory=_now)
    sender: str = ""
    reply_to: Optional[str] = None
    correlation_id: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def event(kind: MessageKind, room_id: str, payload: Dict[str, Any] | None = None,
              sender: str = "agent", seq: int = 0) -> "Envelope":
        return Envelope(type=kind, room_id=room_id, payload=payload or {},
                        sender=sender, seq=seq)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Envelope":
        kind = MessageKind(d["type"])
        return Envelope(
            type=kind,
            v=int(d.get("v", PROTOCOL_VERSION)),
            id=d.get("id", _new_id()),
            room_id=d.get("room_id", ""),
            seq=int(d.get("seq", 0)),
            ts=float(d.get("ts", _now())),
            sender=d.get("sender", ""),
            reply_to=d.get("reply_to"),
            correlation_id=d.get("correlation_id"),
            payload=d.get("payload", {}) or {},
        )


# ---------------------------------------------------------------------------
# RPC request / response (transported inside an Envelope of type RPC/RPC_RESPONSE)
# ---------------------------------------------------------------------------
@dataclass
class RPCError:
    code: str
    message: str


@dataclass
class RPCRequest:
    """Client -> server. Wrapped in an Envelope of type ``rpc``."""

    id: str = field(default_factory=_new_id)
    method: str = ""
    args: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "method": self.method, "args": self.args}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "RPCRequest":
        return RPCRequest(id=d.get("id", _new_id()), method=d.get("method", ""),
                          args=d.get("args", {}) or {})


@dataclass
class RPCResponse:
    """Server -> client. Wrapped in an Envelope of type ``rpc.response``."""

    id: str = field(default_factory=_new_id)
    reply_to: str = ""
    ok: bool = True
    result: Any = None
    error: Optional[RPCError] = None

    @staticmethod
    def success(reply_to: str, result: Any) -> "RPCResponse":
        return RPCResponse(reply_to=reply_to, ok=True, result=result)

    @staticmethod
    def failure(reply_to: str, code: str, message: str) -> "RPCResponse":
        return RPCResponse(reply_to=reply_to, ok=False,
                           error=RPCError(code=code, message=message))

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "id": self.id,
            "reply_to": self.reply_to,
            "ok": self.ok,
            "result": self.result,
        }
        d["error"] = None if self.error is None else asdict(self.error)
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "RPCResponse":
        err = d.get("error")
        return RPCResponse(
            id=d.get("id", _new_id()),
            reply_to=d.get("reply_to", ""),
            ok=bool(d.get("ok", True)),
            result=d.get("result"),
            error=None if err is None else RPCError(code=err.get("code", ""),
                                                    message=err.get("message", "")),
        )


# ---------------------------------------------------------------------------
# RPC method names (single place so backend + frontend + tests agree)
# ---------------------------------------------------------------------------
class RPC:
    ROOM_CREATE = "room.create"
    ROOM_JOIN = "room.join"
    ROOM_LEAVE = "room.leave"
    ROOM_INFO = "room.info"

    AGENT_START = "agent.start"
    AGENT_STATUS = "agent.status"

    STEERING_ACQUIRE = "steering.acquire"
    STEERING_RELEASE = "steering.release"
    STEERING_HANDOFF = "steering.handoff"
    STEERING_PAUSE = "steering.pause"
    STEERING_RESUME = "steering.resume"
    STEERING_INTERRUPT = "steering.interrupt"
    STEERING_INJECT = "steering.inject"
    STEERING_LOCK = "steering.lock"
    STEERING_SET_ENV = "steering.set_env"

    APPROVAL_VOTE = "approval.vote"

    STATE_GRAPH = "state.graph"
    STATE_SNAPSHOT = "state.snapshot"
    STATE_SCRUB = "state.scrub"
    STATE_BRANCH = "state.branch"
    STATE_MERGE = "state.merge"
