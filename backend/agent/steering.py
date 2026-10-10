"""
steering.py — Runtime steering protocol (Pillar A: HITL & Interactive Steering).

Implements the non-blocking control loop:

    RUNNING ──pause()──> PAUSED ──resume()──> RUNNING
       │                    │
       └──interrupt()──> INTERRUPTED (current action abandoned, agent re-plans)
       │
       └──stop()─────> STOPPED (terminal)

Everything is event-driven on asyncio primitives so the engine loop can
`await` a steering signal between steps without blocking the event loop
(non-blocking pause / resume / prompt-injection, per the problem statement).

Steering prompts are *consumed at step boundaries*: the engine reads the
prompt queue at the top of each reasoning step, so a human note typed while
a tool call is in flight lands at the next decision point - exactly the
LangGraph `interrupt()` / resume semantics, but over a shared team channel.
"""
from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Any, Dict, List, Optional
from collections import deque

from ..state import gen_id


class SteerState(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    STOPPED = "stopped"


class SteeringController:
    """Per-room, per-turn steering surface. One instance per running turn."""

    def __init__(self, emit) -> None:
        self.turn_id: Optional[str] = None
        self.state = SteerState.RUNNING
        self._resume = asyncio.Event()
        self._resume.set()                      # start unblocked
        self.prompts: deque = deque()           # ordered human guidance
        self.interrupt_pending = False          # engine abandons in-flight step
        self._emit = emit                       # callable(event_dict) -> None
        self.created = time.time()
        self.history: List[Dict[str, Any]] = [] # audit trail of control events

    # -- control verbs -------------------------------------------------------
    def _set(self, state: SteerState, actor: str, note: str = "") -> None:
        self.state = state
        self.history.append({"ts": time.time(), "state": state.value, "actor": actor, "note": note})
        self._emit({"event": "steer", "state": state.value, "actor": actor, "note": note})

    def pause(self, actor: str = "human", note: str = "paused by human") -> None:
        if self.state in (SteerState.STOPPED,):
            return
        self._resume.clear()
        self.interrupt_pending = False
        self._set(SteerState.PAUSED, actor, note)

    def resume(self, actor: str = "human", note: str = "resumed by human") -> None:
        if self.state == SteerState.STOPPED:
            return
        self.interrupt_pending = False
        self._resume.set()
        self._set(SteerState.RUNNING, actor, note)

    def interrupt(self, actor: str = "human", note: str = "interrupted") -> None:
        """Pause AND abandon the in-flight step; agent must re-plan."""
        self._resume.clear()
        self.interrupt_pending = True
        self._set(SteerState.INTERRUPTED, actor, note)

    def stop(self, actor: str = "human", note: str = "stopped") -> None:
        self._resume.clear()
        self._set(SteerState.STOPPED, actor, note)

    # -- prompt injection ------------------------------------------------------
    def submit_prompt(self, text: str, by: str) -> Dict[str, Any]:
        """Queue a guidance note. Returned immediately (non-blocking)."""
        prompt = {"id": gen_id("prmpt"), "text": text, "by": by, "ts": time.time(), "applied": False}
        self.prompts.append(prompt)
        self._emit({"event": "steer", "prompt": prompt})
        return prompt

    def drain_prompts(self) -> List[Dict[str, Any]]:
        """Engine side: take all queued prompts (marks them applied)."""
        out = list(self.prompts)
        for p in out:
            p["applied"] = True
        self.prompts.clear()
        return out

    # -- engine await ----------------------------------------------------------
    async def wait_if_blocked(self) -> None:
        """Engine calls this at each step boundary. Blocks until unpaused."""
        if not self._resume.is_set():
            self._emit({"event": "steer", "waiting": True})
            await self._resume.wait()

    @property
    def pending_count(self) -> int:
        return len(self.prompts)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "state": self.state.value,
            "interrupt_pending": self.interrupt_pending,
            "pending_prompts": list(self.prompts),
            "last_events": self.history[-5:],
        }
