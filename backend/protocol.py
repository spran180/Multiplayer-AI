"""
protocol.py — Wire protocol for every layer (server ↔ browser clients, server ↔ agent engine).

Server→client events
    room_update   full-or-delta room state (member joins, turns, presence)
    message       one new Message (text/status/action/error/system)
    presence      connected human clients for a room
    steer         steering events (pause, resume, interrupt, confirm, steer, token grant)
    graph         memory-graph versioning events (snapshots, branches, merges)
    done          turn finished (idempotent terminal signal)

Client→server actions
    say           human chat message
    steer_pause / steer_resume / steer_interrupt
    steer_prompt  inject guidance into agent context (non-blocking)
    confirm       approve/deny a gated action
    create_room / join_room / leave_room
    fork_branch   fork the branch the agent is running on, targeting head or an older node
    inspect_state request the stored state at a given branch node id
    merge         merge one branch head into another branch head
    grant_token   human initiates steering-token transfer (auto-resolved)
    ping
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Event:
    event: str
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"event": self.event}
        d.update(self.payload)
        return d


# -- Event constructors (server → client) ------------------------------------

def room_update(room_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    return Event("room_update", {"room_id": room_id, **state}).to_dict()


def message(room_id: str, message_dict: Dict[str, Any]) -> Dict[str, Any]:
    return Event("message", {"room_id": room_id, "message": message_dict}).to_dict()


def presence(room_id: str, clients: List[Dict[str, Any]]) -> Dict[str, Any]:
    return Event("presence", {"room_id": room_id, "clients": clients}).to_dict()


def steer(room_id: str, **kw: Any) -> Dict[str, Any]:
    return Event("steer", {"room_id": room_id, **kw}).to_dict()


def graph(room_id: str, **kw: Any) -> Dict[str, Any]:
    return Event("graph", {"room_id": room_id, **kw}).to_dict()


def done(room_id: str, turn_id: str) -> Dict[str, Any]:
    return Event("done", {"room_id": room_id, "turn_id": turn_id}).to_dict()
