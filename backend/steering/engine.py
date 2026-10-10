"""Steering engine — non-blocking pause / resume / interrupt / prompt-inject.

The agent loop (backend/agent/core.py) *cooperates* with this engine: before
each step it calls :meth:`wait_if_paused`, :meth:`drain_injections`, and
:meth:`should_stop`. That keeps steering non-blocking and race-free — the loop
never fights the engine over state; it just consults it between steps.

Token gating: every control verb validates the current steering token via the
:class:`LockManager`, so only the holder may steer the live run.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, List, Optional

from shared.protocols.python.protocols import RunState, SteeringAction
from .lock import LockManager, SteeringError

# broadcaster(kind: str, payload: dict) -> None
Broadcaster = Optional[Callable[[str, dict], None]]


class SteeringEngine:
    def __init__(self, room_id: str, broadcaster: Broadcaster = None):
        self.room_id = room_id
        self.lock = LockManager(room_id)
        self._state = RunState.IDLE
        self._state_mutex = threading.Lock()
        self.pause_event = threading.Event()      # set => loop halts
        self.interrupt_event = threading.Event()  # set => loop stops
        self.inject_queue: "queue.Queue[str]" = queue.Queue()
        self._broadcaster = broadcaster or (lambda kind, payload: None)

    # -- state ----------------------------------------------------------
    @property
    def state(self) -> RunState:
        with self._state_mutex:
            return self._state

    def set_state(self, state: RunState) -> RunState:
        with self._state_mutex:
            self._state = state
        return state

    # -- lock verbs (no token needed to claim) -------------------------
    def acquire(self, user_id: str, name: Optional[str] = None) -> dict:
        lock = self.lock.acquire(user_id, name)
        self._emit("steering.lock_changed", {"lock": _lock_dict(lock)})
        return {"lock": _lock_dict(lock)}

    def release(self, token: str) -> dict:
        lock = self.lock.release(token)
        self._emit("steering.lock_changed", {"lock": _lock_dict(lock)})
        return {"lock": _lock_dict(lock)}

    def handoff(self, token: str, from_id: str, to_id: str,
                to_name: Optional[str] = None) -> dict:
        lock = self.lock.handoff(token, from_id, to_id, to_name)
        self._emit("steering.lock_changed", {"lock": _lock_dict(lock)})
        return {"lock": _lock_dict(lock)}

    def lock_status(self) -> dict:
        return {"lock": _lock_dict(self.lock.current())}

    # -- control verbs (all require the current token) -----------------
    def pause(self, token: str, actor: str = "") -> RunState:
        self._require(token)
        self.pause_event.set()
        self.set_state(RunState.PAUSED)
        self._emit("steering.status", {"state": RunState.PAUSED.value,
                                       "action": SteeringAction.PAUSE.value, "actor": actor})
        return RunState.PAUSED

    def resume(self, token: str, actor: str = "") -> RunState:
        self._require(token)
        self.pause_event.clear()
        self.set_state(RunState.RUNNING)
        self._emit("steering.status", {"state": RunState.RUNNING.value,
                                       "action": SteeringAction.RESUME.value, "actor": actor})
        return RunState.RUNNING

    def interrupt(self, token: str, actor: str = "", reason: Optional[str] = None) -> RunState:
        self._require(token)
        self.interrupt_event.set()
        self.pause_event.clear()  # unblock a paused loop so it can observe the stop
        self.set_state(RunState.INTERRUPTED)
        self._emit("steering.status", {"state": RunState.INTERRUPTED.value,
                                       "action": SteeringAction.INTERRUPT.value,
                                       "actor": actor, "reason": reason})
        return RunState.INTERRUPTED

    def inject(self, token: str, text: str, actor: str = "") -> dict:
        self._require(token)
        if not text or not text.strip():
            raise SteeringError("invalid_args", "injection text must be non-empty")
        self.inject_queue.put(text.strip())
        # surface the injection as an event so the room sees it immediately
        self._emit("steering.status", {"state": self.state.value,
                                       "action": SteeringAction.INJECT.value,
                                       "actor": actor, "text": text.strip()})
        return {"queued": True, "actor": actor, "ts": time.time()}

    # -- loop-facing ----------------------------------------------------
    def wait_if_paused(self) -> None:
        """Block the agent loop while paused (non-blocking for everyone else)."""
        if self.pause_event.is_set():
            self.pause_event.wait()

    def should_stop(self) -> bool:
        return self.interrupt_event.is_set()

    def drain_injections(self) -> List[str]:
        out: List[str] = []
        while True:
            try:
                out.append(self.inject_queue.get_nowait())
            except queue.Empty:
                return out

    # -- internals ------------------------------------------------------
    def _require(self, token: str) -> None:
        if not self.lock.holds(token):
            raise SteeringError("missing_token", "a valid steering token is required")
        if self.lock.current().holder_id is None:
            raise SteeringError("missing_token", "no current steering holder")

    def _emit(self, kind: str, payload: dict) -> None:
        try:
            self._broadcaster(kind, payload)
        except Exception:  # never let a broadcast failure break steering
            pass

    def reset(self) -> None:
        """Reset run-control signals for a fresh run (keeps the lock)."""
        self.pause_event.clear()
        self.interrupt_event.clear()
        while not self.inject_queue.empty():
            try:
                self.inject_queue.get_nowait()
            except queue.Empty:
                break
        self.set_state(RunState.IDLE)


def _lock_dict(lock) -> dict:
    return {
        "room_id": lock.room_id,
        "holder_id": lock.holder_id,
        "holder_name": lock.holder_name,
        "token": lock.token,
        "acquired_at": lock.acquired_at,
    }
