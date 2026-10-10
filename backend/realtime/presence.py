"""Real-time presence tracking per room."""

from __future__ import annotations

import threading
from typing import Callable, Dict, List, Optional

from shared.protocols.python.protocols import Presence, User


class PresenceTracker:
    def __init__(self, room_id: str, on_change: Optional[Callable[[Presence, str], None]] = None):
        """``on_change(presence, action)`` fires with action in {"joined","left"}."""
        self.room_id = room_id
        self._members: Dict[str, Presence] = {}
        self._mutex = threading.Lock()
        self._on_change = on_change or (lambda presence, action: None)

    def add(self, user: User) -> Presence:
        with self._mutex:
            if user.id not in self._members:
                pres = Presence(user=user, is_host=(len(self._members) == 0))
                self._members[user.id] = pres
                self._notify(pres, "joined")
            return self._members[user.id]

    def remove(self, user_id: str) -> Optional[Presence]:
        with self._mutex:
            pres = self._members.pop(user_id, None)
            if pres is not None:
                self._notify(pres, "left")
            return pres

    def get(self, user_id: str) -> Optional[Presence]:
        with self._mutex:
            return self._members.get(user_id)

    def list(self) -> List[Presence]:
        with self._mutex:
            return list(self._members.values())

    def count(self) -> int:
        with self._mutex:
            return len(self._members)

    def to_dict(self) -> List[dict]:
        from dataclasses import asdict
        return [asdict(p) for p in self.list()]

    def _notify(self, presence: Presence, action: str) -> None:
        from dataclasses import asdict
        try:
            self._on_change(asdict(presence), action)
        except Exception:
            pass
