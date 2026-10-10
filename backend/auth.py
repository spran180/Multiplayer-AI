"""Account, session, and room-membership storage shared through Redis when configured."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import closing


SESSION_TTL_SECONDS = 60 * 60 * 24 * 30
PASSWORD_ITERATIONS = 310_000


class AuthError(ValueError):
    pass


def _email_key(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthStore:
    """Use Redis for multi-replica deployments, SQLite for local development."""

    def __init__(self, redis_url: str = "", sqlite_path: str = "data/state.sqlite3"):
        self._lock = threading.RLock()
        self._redis = None
        if redis_url:
            import redis
            self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
            self._redis.ping()
        else:
            self.sqlite_path = sqlite_path
            os.makedirs(os.path.dirname(os.path.abspath(sqlite_path)), exist_ok=True)
            with closing(sqlite3.connect(sqlite_path)) as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS auth_users (
                        user_id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                        display_name TEXT NOT NULL, password_salt TEXT NOT NULL,
                        password_hash TEXT NOT NULL, created_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS auth_sessions (
                        token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                        expires_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS room_memberships (
                        room_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL,
                        PRIMARY KEY(room_id, user_id)
                    );
                    CREATE TABLE IF NOT EXISTS room_invites (
                        room_id TEXT NOT NULL, token_hash TEXT NOT NULL,
                        created_by TEXT NOT NULL, created_at REAL NOT NULL,
                        PRIMARY KEY(room_id, token_hash)
                    );
                """)
                db.commit()

    def close(self):
        if self._redis:
            self._redis.close()

    @staticmethod
    def _password(password: str, salt: bytes) -> str:
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt,
                                   PASSWORD_ITERATIONS).hex()

    def register(self, email: str, password: str, display_name: str) -> dict:
        email = email.strip().lower()
        display_name = display_name.strip()[:80]
        if len(email) > 254 or "@" not in email or email.startswith("@") or email.endswith("@"):
            raise AuthError("Enter a valid email address.")
        if len(password) < 10 or len(password) > 256:
            raise AuthError("Password must be between 10 and 256 characters.")
        if not display_name:
            raise AuthError("Enter your name.")
        user_id = uuid.uuid4().hex
        salt = secrets.token_bytes(16)
        user = {"id": user_id, "email": email, "name": display_name,
                "salt": salt.hex(), "password_hash": self._password(password, salt),
                "created_at": time.time()}
        if self._redis:
            key = f"multiplayer-agent:auth:email:{_email_key(email)}"
            user_key = f"multiplayer-agent:auth:user:{user_id}"
            created = self._redis.eval("""
                if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
                redis.call('SET', KEYS[1], ARGV[1])
                redis.call('SET', KEYS[2], ARGV[2])
                return 1
            """, 2, key, user_key, user_id, json.dumps(user))
            if not created:
                raise AuthError("An account with this email already exists.")
        else:
            try:
                with self._lock, closing(sqlite3.connect(self._sqlite_path())) as db:
                    db.execute("INSERT INTO auth_users VALUES(?,?,?,?,?,?)", (
                        user_id, email, display_name, salt.hex(), user["password_hash"], user["created_at"]))
                    db.commit()
            except sqlite3.IntegrityError as exc:
                raise AuthError("An account with this email already exists.") from exc
        return self._public_user(user)

    def _sqlite_path(self) -> str:
        return self.sqlite_path

    @staticmethod
    def _public_user(user: dict) -> dict:
        return {"id": user["id"], "email": user["email"], "name": user["name"]}

    def authenticate(self, email: str, password: str) -> dict | None:
        email = email.strip().lower()
        if self._redis:
            user_id = self._redis.get(f"multiplayer-agent:auth:email:{_email_key(email)}")
            raw = self._redis.get(f"multiplayer-agent:auth:user:{user_id}") if user_id else None
            user = json.loads(raw) if raw else None
        else:
            with self._lock, closing(sqlite3.connect(self._sqlite_path())) as db:
                db.row_factory = sqlite3.Row
                row = db.execute("SELECT * FROM auth_users WHERE email=?", (email,)).fetchone()
                user = ({"id": row["user_id"], "email": row["email"],
                         "name": row["display_name"], "salt": row["password_salt"],
                         "password_hash": row["password_hash"]} if row else None)
        if not user:
            return None
        actual = self._password(password, bytes.fromhex(user["salt"]))
        return self._public_user(user) if hmac.compare_digest(actual, user["password_hash"]) else None

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(40)
        digest = _token_hash(token)
        if self._redis:
            self._redis.set(f"multiplayer-agent:auth:session:{digest}", user_id,
                            ex=SESSION_TTL_SECONDS)
        else:
            with self._lock, closing(sqlite3.connect(self._sqlite_path())) as db:
                db.execute("INSERT INTO auth_sessions VALUES(?,?,?)",
                           (digest, user_id, time.time() + SESSION_TTL_SECONDS))
                db.commit()
        return token

    def get_session_user(self, token: str | None) -> dict | None:
        if not token:
            return None
        digest = _token_hash(token)
        if self._redis:
            user_id = self._redis.get(f"multiplayer-agent:auth:session:{digest}")
            raw = self._redis.get(f"multiplayer-agent:auth:user:{user_id}") if user_id else None
            return self._public_user(json.loads(raw)) if raw else None
        with self._lock, closing(sqlite3.connect(self._sqlite_path())) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT u.* FROM auth_sessions s JOIN auth_users u USING(user_id) "
                             "WHERE s.token_hash=? AND s.expires_at>?", (digest, time.time())).fetchone()
            return ({"id": row["user_id"], "email": row["email"], "name": row["display_name"]}
                    if row else None)

    def revoke_session(self, token: str | None):
        if not token:
            return
        digest = _token_hash(token)
        if self._redis:
            self._redis.delete(f"multiplayer-agent:auth:session:{digest}")
        else:
            with self._lock, closing(sqlite3.connect(self._sqlite_path())) as db:
                db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (digest,))
                db.commit()

    def add_room_owner(self, room_id: str, user_id: str) -> str:
        self._add_member(room_id, user_id, "owner")
        return self.create_invite(room_id, user_id)

    def claim_legacy_room(self, room_id: str, user_id: str) -> bool:
        """Let exactly one authenticated user claim a pre-auth room."""
        if self._redis:
            return bool(self._redis.eval("""
                local members = KEYS[1]
                if redis.call('EXISTS', members) == 1 then return 0 end
                redis.call('HSET', members, ARGV[1], 'owner')
                redis.call('SADD', KEYS[2], ARGV[2])
                return 1
            """, 2, f"multiplayer-agent:auth:room:{room_id}:members",
                f"multiplayer-agent:auth:user:{user_id}:rooms", user_id, room_id))
        with self._lock, closing(sqlite3.connect(self._sqlite_path())) as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute("SELECT 1 FROM room_memberships WHERE room_id=? LIMIT 1",
                                (room_id,)).fetchone()
            if exists:
                db.rollback()
                return False
            db.execute("INSERT INTO room_memberships VALUES(?,?, 'owner')", (room_id, user_id))
            db.commit()
            return True

    def _add_member(self, room_id: str, user_id: str, role: str = "member"):
        if self._redis:
            self._redis.hset(f"multiplayer-agent:auth:room:{room_id}:members", user_id, role)
            self._redis.sadd(f"multiplayer-agent:auth:user:{user_id}:rooms", room_id)
        else:
            with self._lock, closing(sqlite3.connect(self._sqlite_path())) as db:
                db.execute("INSERT OR IGNORE INTO room_memberships VALUES(?,?,?)", (room_id, user_id, role))
                db.commit()

    def create_invite(self, room_id: str, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        digest = _token_hash(token)
        if self._redis:
            self._redis.sadd(f"multiplayer-agent:auth:room:{room_id}:invites", digest)
        else:
            with self._lock, closing(sqlite3.connect(self._sqlite_path())) as db:
                db.execute("INSERT INTO room_invites VALUES(?,?,?,?)",
                           (room_id, digest, user_id, time.time()))
                db.commit()
        return token

    def has_members(self, room_id: str) -> bool:
        if self._redis:
            return bool(self._redis.exists(f"multiplayer-agent:auth:room:{room_id}:members"))
        with self._lock, closing(sqlite3.connect(self._sqlite_path())) as db:
            return db.execute("SELECT 1 FROM room_memberships WHERE room_id=? LIMIT 1", (room_id,)).fetchone() is not None

    def authorize_room(self, room_id: str, user_id: str, invite: str = "") -> bool:
        if self._redis:
            key = f"multiplayer-agent:auth:room:{room_id}:members"
            if self._redis.hexists(key, user_id):
                return True
            if invite and self._redis.sismember(f"multiplayer-agent:auth:room:{room_id}:invites", _token_hash(invite)):
                self._add_member(room_id, user_id)
                return True
            return False
        with self._lock, closing(sqlite3.connect(self._sqlite_path())) as db:
            member = db.execute("SELECT 1 FROM room_memberships WHERE room_id=? AND user_id=?",
                                (room_id, user_id)).fetchone()
            if member:
                return True
            if invite and db.execute("SELECT 1 FROM room_invites WHERE room_id=? AND token_hash=?",
                                     (room_id, _token_hash(invite))).fetchone():
                db.execute("INSERT OR IGNORE INTO room_memberships VALUES(?,?, 'member')", (room_id, user_id))
                db.commit()
                return True
        return False

    def room_ids_for_user(self, user_id: str) -> list[str]:
        if self._redis:
            return sorted(self._redis.smembers(f"multiplayer-agent:auth:user:{user_id}:rooms"))
        with self._lock, closing(sqlite3.connect(self._sqlite_path())) as db:
            return [r[0] for r in db.execute("SELECT room_id FROM room_memberships WHERE user_id=?", (user_id,))]

    def owns_room(self, room_id: str, user_id: str) -> bool:
        if self._redis:
            return self._redis.hget(f"multiplayer-agent:auth:room:{room_id}:members", user_id) == "owner"
        with self._lock, closing(sqlite3.connect(self._sqlite_path())) as db:
            return db.execute("SELECT 1 FROM room_memberships WHERE room_id=? AND user_id=? AND role='owner'",
                              (room_id, user_id)).fetchone() is not None
