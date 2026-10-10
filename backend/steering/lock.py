"""Steering token lock — the concurrency-control primitive for control hand-off.

A room has exactly one steering holder at a time. The credential is a rotating
``token``. Every mutating operation (pause/resume/interrupt/inject) and every
hand-off validates the *current* token via compare-and-swap, so two clients can
never both hold steering rights even if their requests interleave.

Thread-safety: a single internal ``threading.Lock`` serializes mutations.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Optional

from shared.protocols.python.protocols import SteeringLock


class SteeringError(Exception):
    """Carries a stable ``code`` so RPC clients can branch on failures."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _new_token() -> str:
    return uuid.uuid4().hex


class LockManager:
    def __init__(self, room_id: str):
        self.room_id = room_id
        self._mutex = threading.RLock()
        self.holder_id: Optional[str] = None
        self.holder_name: Optional[str] = None
        self.token: str = _new_token()
        self.acquired_at: float = time.time()

    # -- read -----------------------------------------------------------
    def current(self) -> SteeringLock:
        with self._mutex:
            return SteeringLock(
                room_id=self.room_id,
                holder_id=self.holder_id,
                holder_name=self.holder_name,
                token=self.token,
                acquired_at=self.acquired_at,
            )

    def holds(self, token: str, user_id: Optional[str] = None) -> bool:
        with self._mutex:
            if self.token != token:
                return False
            if user_id is not None and self.holder_id != user_id:
                return False
            return True

    # -- mutations (each a CAS) ----------------------------------------
    def acquire(self, user_id: str, name: Optional[str] = None) -> SteeringLock:
        """Claim steering rights for ``user_id``.

        Re-acquire by the same holder is idempotent (keeps the token); a
        different user must receive an explicit hand-off from the holder.
        """
        with self._mutex:
            if self.holder_id is not None and self.holder_id != user_id:
                raise SteeringError("lock_held", "steering is held by another room member")
            if self.holder_id != user_id:
                self.token = _new_token()
            self.holder_id = user_id
            self.holder_name = name
            self.acquired_at = time.time()
            return self.current()

    def release(self, token: str) -> SteeringLock:
        with self._mutex:
            if self.token != token:
                raise SteeringError("wrong_token", "steering token no longer valid")
            self.holder_id = None
            self.holder_name = None
            self.token = _new_token()
            self.acquired_at = time.time()
            return self.current()

    def handoff(self, token: str, from_id: str, to_id: str,
                to_name: Optional[str] = None) -> SteeringLock:
        """Atomically transfer steering rights from ``from_id`` to ``to_id``."""
        if from_id == to_id:
            raise SteeringError("invalid_args", "hand-off target must differ from source")
        with self._mutex:
            if self.token != token or self.holder_id != from_id:
                raise SteeringError("wrong_token", "only the current holder may hand off")
            self.holder_id = to_id
            self.holder_name = to_name
            self.token = _new_token()
            self.acquired_at = time.time()
            return self.current()


class RedisLockManager:
    """CAS steering lock shared by every API replica for a room."""

    _ACQUIRE = """
    local holder = redis.call('HGET', KEYS[1], 'holder_id')
    if holder and holder ~= ARGV[1] then return {0} end
    local token = redis.call('HGET', KEYS[1], 'token')
    if not holder or holder == '' then token = ARGV[3] end
    redis.call('HSET', KEYS[1], 'holder_id', ARGV[1], 'holder_name', ARGV[2],
        'token', token, 'acquired_at', ARGV[4])
    return {1, token, ARGV[4]}
    """
    _RELEASE = """
    if redis.call('HGET', KEYS[1], 'token') ~= ARGV[1] then return 0 end
    redis.call('HSET', KEYS[1], 'holder_id', '', 'holder_name', '',
        'token', ARGV[2], 'acquired_at', ARGV[3])
    return 1
    """
    _HANDOFF = """
    if redis.call('HGET', KEYS[1], 'token') ~= ARGV[1] or
       redis.call('HGET', KEYS[1], 'holder_id') ~= ARGV[2] then return 0 end
    redis.call('HSET', KEYS[1], 'holder_id', ARGV[3], 'holder_name', ARGV[4],
        'token', ARGV[5], 'acquired_at', ARGV[6])
    return 1
    """

    def __init__(self, room_id: str, client, prefix: str = "multiplayer-agent"):
        self.room_id = room_id
        self.client = client
        self.key = f"{prefix}:room:{room_id}:steering-lock"

    def current(self) -> SteeringLock:
        data = self.client.hgetall(self.key)
        return SteeringLock(
            room_id=self.room_id,
            holder_id=data.get("holder_id") or None,
            holder_name=data.get("holder_name") or None,
            token=data.get("token") or "unclaimed",
            acquired_at=float(data.get("acquired_at") or time.time()),
        )

    def holds(self, token: str, user_id: Optional[str] = None) -> bool:
        data = self.client.hmget(self.key, "token", "holder_id")
        return bool(data[0] == token and data[1] and (user_id is None or data[1] == user_id))

    def acquire(self, user_id: str, name: Optional[str] = None) -> SteeringLock:
        now = str(time.time())
        result = self.client.eval(self._ACQUIRE, 1, self.key, user_id, name or "", _new_token(), now)
        if not result or int(result[0]) == 0:
            raise SteeringError("lock_held", "steering is held by another room member")
        return self.current()

    def release(self, token: str) -> SteeringLock:
        result = self.client.eval(self._RELEASE, 1, self.key, token, _new_token(), str(time.time()))
        if not result:
            raise SteeringError("wrong_token", "steering token no longer valid")
        return self.current()

    def handoff(self, token: str, from_id: str, to_id: str,
                to_name: Optional[str] = None) -> SteeringLock:
        if from_id == to_id:
            raise SteeringError("invalid_args", "hand-off target must differ from source")
        result = self.client.eval(self._HANDOFF, 1, self.key, token, from_id, to_id,
                                  to_name or "", _new_token(), str(time.time()))
        if not result:
            raise SteeringError("wrong_token", "only the current holder may hand off")
        return self.current()

