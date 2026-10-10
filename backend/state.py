"""
state.py — Canonical shared state schema: rooms, agents, messages, turns.

Everything in the system flows through this contract. The browser client and the
WebSocket server both speak this JSON, so a change here is a change every layer
must understand.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# IDs
# ---------------------------------------------------------------------------

def gen_id(prefix: str) -> str:
    """Collision-resistant short id: <prefix>_<timestamp>_<hex>."""
    return f"{prefix}_{int(time.time() * 1000)}x{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Role(str, Enum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


class TurnState(str, Enum):
    """Lifecycle of an agent's single reasoning turn within a room."""
    IDLE = "idle"            # agent not active in this room
    THINKING = "thinking"    # LLM deciding what to say / do
    ACTING = "acting"        # tool / action in flight
    BLOCKED = "blocked"      # waiting on human input (interrupt / confirm)
    DONE = "done"            # turn complete; agent idle again


class MessageKind(str, Enum):
    TEXT = "text"            # plain chat message
    STATUS = "status"        # agent thought / narration
    ACTION = "action"        # structured action block (rendered specially)
    ERROR = "error"
    SYSTEM = "system"        # room-level system event (join, topic, ...)


class RoomState(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


# ---------------------------------------------------------------------------
# Data classes (server-side truth; .to_dict() is the wire format)
# ---------------------------------------------------------------------------

@dataclass
class Member:
    """A participant: a human user or an agent (with optional policy constraints)."""
    id: str
    name: str
    role: Role
    # For agents (Sapien-style guardrails): allowed tool names (None = no restriction)
    allowed_tools: Optional[List[str]] = None
    # For agents: max tool calls per turn (None = default)
    max_tool_calls: Optional[int] = None
    # For agents: max reasoning iterations per turn (None = default)
    max_iterations: Optional[int] = None
    # Arbitrary per-member metadata (e.g. avatar, model name, system prompt)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role.value,
            "allowed_tools": self.allowed_tools,
            "max_tool_calls": self.max_tool_calls,
            "max_iterations": self.max_iterations,
            "meta": self.meta,
        }


@dataclass
class Message:
    id: str
    room_id: str
    sender_id: str
    sender_name: str
    role: Role
    kind: MessageKind
    content: str
    ts: str
    # For ACTION blocks: the structured payload the UI renders
    action: Optional[Dict[str, Any]] = None
    # For STATUS blocks: the agent's thinking snippet (hidden/collapsible)
    thinking: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self.id,
            "room_id": self.room_id,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "role": self.role.value,
            "kind": self.kind.value,
            "content": self.content,
            "ts": self.ts,
        }
        if self.action is not None:
            d["action"] = self.action
        if self.thinking is not None:
            d["thinking"] = self.thinking
        return d


@dataclass
class Room:
    id: str
    name: str
    topic: str = ""
    state: RoomState = RoomState.ACTIVE
    created: str = field(default_factory=lambda: _now())
    members: List[Member] = field(default_factory=list)
    messages: List[Message] = field(default_factory=list)
    # Shared scratch state for agents (agent memory, Sapien-style variable bindings)
    shared: Dict[str, Any] = field(default_factory=dict)
    # Human-initiated steering: pending intent instructions injected into agent context
    steering: List[Dict[str, Any]] = field(default_factory=list)
    # Concurrent turns: dict of {turn_id: {"state": ..., "agent_id": ..., "started": ...}}
    turns: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self, include_history: bool = True) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "topic": self.topic,
            "state": self.state.value,
            "created": self.created,
            "members": [m.to_dict() for m in self.members],
            "steering": self.steering,
            "turns": {
                tid: {
                    "state": t["state"],
                    "agent_id": t["agent_id"],
                    "agent_name": t["agent_name"],
                    "started": t["started"],
                }
                for tid, t in self.turns.items()
            },
            "shared": self.shared,
        }
        if include_history:
            d["messages"] = [m.to_dict() for m in self.messages]
        return d

    # convenience -------------------------------------------------------------
    def member(self, member_id: str) -> Optional[Member]:
        for m in self.members:
            if m.id == member_id:
                return m
        return None

    def agent_members(self) -> List[Member]:
        return [m for m in self.members if m.role == Role.AGENT]

    def agents_by_name(self, name: str) -> List[Member]:
        return [m for m in self.members if m.role == Role.AGENT and m.name.lower() == name.lower()]


def _now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
