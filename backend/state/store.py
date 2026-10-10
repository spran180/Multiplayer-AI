"""SQLite persistence for state graphs.

Single-file database, one row-space per room. Writes are serialized behind a
lock (sqlite connection is shared across threads with check_same_thread=False).
At this platform's scale (a room's execution graph is small) we upsert the
whole graph on commit — simple, correct, and fast enough.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from typing import List, Optional

from shared.protocols.python.protocols import MemoryEntry, RunState, Snapshot
from .graph import StateGraph

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    room_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    name TEXT NOT NULL DEFAULT 'Shared agent room'
);
CREATE TABLE IF NOT EXISTS graph_meta (
    room_id TEXT PRIMARY KEY,
    root_id TEXT,
    head_id TEXT
);
CREATE TABLE IF NOT EXISTS graph_nodes (
    room_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    step_index INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'step',
    snapshot_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (room_id, node_id)
);
CREATE TABLE IF NOT EXISTS node_parents (
    room_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (room_id, node_id, seq)
);
CREATE TABLE IF NOT EXISTS snapshots (
    room_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    node_id TEXT NOT NULL DEFAULT '',
    step_index INTEGER NOT NULL DEFAULT 0,
    run_state TEXT NOT NULL DEFAULT 'running',
    memory TEXT NOT NULL DEFAULT '[]',
    scratch TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (room_id, snapshot_id)
);
"""


class StateStore:
    def __init__(self, path: str = "state.db"):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(rooms)")}
            if "name" not in columns:
                self._conn.execute("ALTER TABLE rooms ADD COLUMN name TEXT NOT NULL DEFAULT 'Shared agent room'")
            self._conn.commit()
        self._runs = {}
        self._members = {}
        self._approvals = {}
        self._run_states = {}
        self._runtime_env = {}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- rooms ----------------------------------------------------------
    def create_room(self, room_id: str, created_at: float, name: str = "Shared agent room") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO rooms(room_id, created_at, name) VALUES(?, ?, ?)",
                (room_id, created_at, name),
            )
            self._conn.commit()

    def list_rooms(self) -> List[str]:
        with self._lock:
            rows = self._conn.execute("SELECT room_id FROM rooms ORDER BY created_at").fetchall()
        return [r["room_id"] for r in rows]

    def room_metadata(self, room_id: str) -> dict:
        with self._lock:
            row = self._conn.execute("SELECT * FROM rooms WHERE room_id=?", (room_id,)).fetchone()
        return dict(row) if row else {}

    def add_member(self, room_id: str, member: dict) -> None:
        with self._lock:
            self._members.setdefault(room_id, {})[member["id"]] = dict(member)

    def remove_member(self, room_id: str, user_id: str) -> None:
        with self._lock:
            self._members.get(room_id, {}).pop(user_id, None)

    def list_members(self, room_id: str) -> List[dict]:
        with self._lock:
            return list(self._members.get(room_id, {}).values())

    def acquire_run(self, room_id: str, ttl_seconds: int = 3600) -> Optional[str]:
        del ttl_seconds
        with self._lock:
            if room_id in self._runs:
                return None
            token = uuid.uuid4().hex
            self._runs[room_id] = token
            return token

    def release_run(self, room_id: str, token: str) -> None:
        with self._lock:
            if self._runs.get(room_id) == token:
                self._runs.pop(room_id, None)

    def run_is_active(self, room_id: str) -> bool:
        with self._lock:
            return room_id in self._runs

    def renew_run(self, room_id: str, token: str, ttl_seconds: int = 3600) -> bool:
        del ttl_seconds
        with self._lock:
            return self._runs.get(room_id) == token

    def set_run_state(self, room_id: str, state: str) -> None:
        with self._lock:
            self._run_states[room_id] = state

    def get_run_state(self, room_id: str) -> Optional[str]:
        with self._lock:
            return self._run_states.get(room_id)

    def set_runtime_env(self, room_id: str, key: str, value: str) -> None:
        with self._lock:
            self._runtime_env.setdefault(room_id, {})[key] = value

    def get_runtime_env(self, room_id: str) -> dict:
        with self._lock:
            return dict(self._runtime_env.get(room_id, {}))

    def create_approval(self, gate_id: str, room_id: str, requested_by: str,
                        action: dict, required: int, eligible_voters=None,
                        requested_quorum=None, self_approval_allowed=False) -> dict:
        with self._lock:
            gate = {"gate_id": gate_id, "room_id": room_id, "requested_by": requested_by,
                    "action": action, "required": required, "votes": [], "status": "pending",
                    "eligible_voters": list(eligible_voters or []),
                    "requested_quorum": requested_quorum or required,
                    "self_approval_allowed": bool(self_approval_allowed)}
            self._approvals[gate_id] = gate
            return dict(gate)

    def get_approval(self, gate_id: str) -> Optional[dict]:
        with self._lock:
            gate = self._approvals.get(gate_id)
            return dict(gate) if gate else None

    def list_approvals(self, room_id: str) -> List[dict]:
        with self._lock:
            return [dict(gate) for gate in self._approvals.values()
                    if gate.get("room_id") == room_id and gate.get("status") == "pending"]

    def cast_approval_vote(self, gate_id: str, voter_id: str, voter_name: str,
                           approve: bool, reason: str = "", active_voters=None) -> dict:
        with self._lock:
            gate = self._approvals.get(gate_id)
            if not gate:
                raise KeyError("approval gate not found")
            if gate["status"] != "pending":
                raise ValueError("approval gate is already closed")
            eligible = gate.get("eligible_voters", [])
            if voter_id not in eligible or (active_voters is not None and voter_id not in active_voters):
                raise ValueError("you are not an eligible reviewer for this action")
            if any(v["user_id"] == voter_id for v in gate["votes"]):
                raise ValueError("this participant has already voted")
            gate["votes"].append({"user_id": voter_id, "name": voter_name,
                                  "approve": bool(approve), "reason": reason[:500]})
            approvals = sum(1 for vote in gate["votes"] if vote["approve"])
            if not approve:
                gate["status"] = "rejected"
            elif approvals >= gate["required"]:
                gate["status"] = "approved"
            return dict(gate)

    def reconcile_approvals(self, room_id: str, active_voters) -> List[dict]:
        changed = []
        active = set(active_voters)
        with self._lock:
            for gate in self._approvals.values():
                if gate.get("room_id") != room_id or gate.get("status") != "pending":
                    continue
                yes = sum(1 for vote in gate["votes"] if vote["approve"])
                possible = len(active.intersection(gate.get("eligible_voters", [])))
                next_required = min(gate["required"], max(1, possible, yes))
                if next_required != gate["required"]:
                    gate["required"] = next_required
                    if yes >= next_required:
                        gate["status"] = "approved"
                    changed.append(dict(gate))
        return changed

    def expire_approval(self, gate_id: str) -> Optional[dict]:
        with self._lock:
            gate = self._approvals.get(gate_id)
            if gate and gate["status"] == "pending":
                gate["status"] = "expired"
            return dict(gate) if gate else None

    # -- save -----------------------------------------------------------
    def save(self, graph: StateGraph) -> None:
        with self._lock:
            cur = self._conn.cursor()
            self.create_room_locked(graph.room_id, _now())
            cur.execute(
                "INSERT INTO graph_meta(room_id, root_id, head_id) VALUES(?,?,?) "
                "ON CONFLICT(room_id) DO UPDATE SET root_id=excluded.root_id, head_id=excluded.head_id",
                (graph.room_id, graph.root_id, graph.head_id),
            )
            # nodes + parents (replace parents for this room)
            for n in graph.nodes.values():
                cur.execute(
                    """INSERT INTO graph_nodes
                       (room_id,node_id,label,step_index,kind,snapshot_id,created_at,meta)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(room_id,node_id) DO UPDATE SET
                         label=excluded.label, step_index=excluded.step_index,
                         kind=excluded.kind, snapshot_id=excluded.snapshot_id,
                         created_at=excluded.created_at, meta=excluded.meta""",
                    (graph.room_id, n.node_id, n.label, n.step_index, n.kind,
                     n.snapshot_id, n.created_at, json.dumps(n.meta)),
                )
                cur.execute(
                    "DELETE FROM node_parents WHERE room_id=? AND node_id=?",
                    (graph.room_id, n.node_id),
                )
                for i, p in enumerate(n.parents):
                    cur.execute(
                        "INSERT INTO node_parents(room_id,node_id,parent_id,seq) VALUES(?,?,?,?)",
                        (graph.room_id, n.node_id, p, i),
                    )
            for sid, s in graph.snapshots.items():
                cur.execute(
                    """INSERT INTO snapshots
                       (room_id,snapshot_id,node_id,step_index,run_state,memory,scratch,content_hash,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(room_id,snapshot_id) DO UPDATE SET
                         node_id=excluded.node_id, step_index=excluded.step_index,
                         run_state=excluded.run_state, memory=excluded.memory,
                         scratch=excluded.scratch, content_hash=excluded.content_hash,
                         created_at=excluded.created_at""",
                    (graph.room_id, sid, s.node_id, s.step_index, _rs_value(s.run_state),
                     json.dumps([_mem_to_dict(m) for m in s.memory]),
                     json.dumps(s.scratch), s.content_hash, s.created_at),
                )
            self._conn.commit()

    def create_room_locked(self, room_id: str, created_at: float) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO rooms(room_id, created_at, name) VALUES(?, ?, ?)",
            (room_id, created_at, "Shared agent room"),
        )

    # -- load -----------------------------------------------------------
    def load(self, room_id: str) -> StateGraph:
        g = StateGraph(room_id=room_id)
        with self._lock:
            meta = self._conn.execute(
                "SELECT root_id, head_id FROM graph_meta WHERE room_id=?", (room_id,)
            ).fetchone()
            if meta:
                g.root_id, g.head_id = meta["root_id"], meta["head_id"]
            snap_rows = self._conn.execute(
                "SELECT * FROM snapshots WHERE room_id=?", (room_id,)
            ).fetchall()
            for r in snap_rows:
                g.snapshots[r["snapshot_id"]] = _snapshot_from_row(r)
            node_rows = self._conn.execute(
                "SELECT * FROM graph_nodes WHERE room_id=?", (room_id,)
            ).fetchall()
            parent_rows = self._conn.execute(
                "SELECT * FROM node_parents WHERE room_id=? ORDER BY node_id, seq", (room_id,)
            ).fetchall()
        parent_map = {}
        for r in parent_rows:
            parent_map.setdefault(r["node_id"], []).append(r["parent_id"])
        from shared.protocols.python.protocols import GraphNode
        for r in node_rows:
            g.nodes[r["node_id"]] = GraphNode(
                node_id=r["node_id"], room_id=r["room_id"], label=r["label"],
                parents=parent_map.get(r["node_id"], []), step_index=r["step_index"],
                kind=r["kind"], snapshot_id=r["snapshot_id"], created_at=r["created_at"],
                meta=json.loads(r["meta"] or "{}"),
            )
        return g

    def has_room(self, room_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM rooms WHERE room_id=?", (room_id,)
            ).fetchone()
        return row is not None


# -- row <-> object helpers -------------------------------------------
def _now() -> float:
    import time
    return time.time()


def _rs_value(rs) -> str:
    return rs.value if isinstance(rs, RunState) else str(rs)


def _mem_to_dict(m: MemoryEntry) -> dict:
    return {"kind": m.kind, "text": m.text}


def _snapshot_from_row(r: sqlite3.Row) -> Snapshot:
    mem = [MemoryEntry(kind=m["kind"], text=m["text"]) for m in json.loads(r["memory"] or "[]")]
    return Snapshot(
        snapshot_id=r["snapshot_id"], node_id=r["node_id"], step_index=r["step_index"],
        run_state=RunState(r["run_state"]), memory=mem,
        scratch=json.loads(r["scratch"] or "{}"), content_hash=r["content_hash"],
        created_at=r["created_at"],
    )


class RedisStateStore:
    """Shared graph store and run lease for multiple API replicas."""

    def __init__(self, url: str):
        import redis
        self.client = redis.Redis.from_url(url, decode_responses=True)
        self.client.ping()
        self.prefix = "multiplayer-agent"

    def _room_key(self, room_id: str, suffix: str) -> str:
        return f"{self.prefix}:room:{room_id}:{suffix}"

    def close(self):
        self.client.close()

    def create_room(self, room_id: str, created_at: float, name: str = "Shared agent room") -> None:
        pipe = self.client.pipeline(transaction=True)
        pipe.sadd(f"{self.prefix}:rooms", room_id)
        pipe.set(self._room_key(room_id, "metadata"), json.dumps({
            "room_id": room_id, "created_at": created_at, "name": name,
        }), nx=True)
        pipe.execute()

    def list_rooms(self) -> List[str]:
        return sorted(self.client.smembers(f"{self.prefix}:rooms"))

    def room_metadata(self, room_id: str) -> dict:
        raw = self.client.get(self._room_key(room_id, "metadata"))
        return json.loads(raw) if raw else {}

    def _members_key(self, room_id: str) -> str:
        return self._room_key(room_id, "members")

    def _member_key(self, room_id: str, user_id: str) -> str:
        return self._room_key(room_id, f"member:{user_id}")

    def add_member(self, room_id: str, member: dict) -> None:
        key = self._member_key(room_id, member["id"])
        pipe = self.client.pipeline(transaction=True)
        pipe.set(key, json.dumps(member), ex=86400)
        pipe.sadd(self._members_key(room_id), member["id"])
        pipe.execute()

    def remove_member(self, room_id: str, user_id: str) -> None:
        pipe = self.client.pipeline(transaction=True)
        pipe.delete(self._member_key(room_id, user_id))
        pipe.srem(self._members_key(room_id), user_id)
        pipe.execute()

    def list_members(self, room_id: str) -> List[dict]:
        ids = self.client.smembers(self._members_key(room_id))
        if not ids:
            return []
        values = self.client.mget([self._member_key(room_id, user_id) for user_id in ids])
        members = []
        stale = []
        for user_id, raw in zip(ids, values):
            if raw:
                members.append(json.loads(raw))
            else:
                stale.append(user_id)
        if stale:
            self.client.srem(self._members_key(room_id), *stale)
        return members

    def has_room(self, room_id: str) -> bool:
        return bool(self.client.sismember(f"{self.prefix}:rooms", room_id))

    def load(self, room_id: str) -> StateGraph:
        raw = self.client.get(self._room_key(room_id, "graph"))
        return StateGraph.from_dict(json.loads(raw)) if raw else StateGraph(room_id)

    def save(self, graph: StateGraph) -> None:
        key = self._room_key(graph.room_id, "graph")
        lock = self.client.lock(self._room_key(graph.room_id, "graph-lock"), timeout=10,
                                blocking_timeout=5)
        with lock:
            latest = self.load(graph.room_id)
            if latest.nodes:
                for node_id, node in graph.nodes.items():
                    latest.nodes[node_id] = node
                latest.snapshots.update(graph.snapshots)
                latest.root_id = latest.root_id or graph.root_id
                latest.head_id = graph.head_id or latest.head_id
                graph = latest
            pipe = self.client.pipeline(transaction=True)
            pipe.sadd(f"{self.prefix}:rooms", graph.room_id)
            pipe.set(key, json.dumps(graph.to_dict(), default=str))
            pipe.execute()

    def acquire_run(self, room_id: str, ttl_seconds: int = 3600) -> Optional[str]:
        token = uuid.uuid4().hex
        acquired = self.client.set(self._room_key(room_id, "active-run"), token,
                                   nx=True, ex=ttl_seconds)
        return token if acquired else None

    def release_run(self, room_id: str, token: str) -> None:
        self.client.eval(
            "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end",
            1, self._room_key(room_id, "active-run"), token,
        )

    def run_is_active(self, room_id: str) -> bool:
        return self.client.exists(self._room_key(room_id, "active-run")) == 1

    def renew_run(self, room_id: str, token: str, ttl_seconds: int = 3600) -> bool:
        return bool(self.client.eval(
            "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('EXPIRE', KEYS[1], ARGV[2]) else return 0 end",
            1, self._room_key(room_id, "active-run"), token, ttl_seconds,
        ))

    def set_run_state(self, room_id: str, state: str) -> None:
        self.client.set(self._room_key(room_id, "run-state"), state, ex=86400)

    def get_run_state(self, room_id: str) -> Optional[str]:
        return self.client.get(self._room_key(room_id, "run-state"))

    def set_runtime_env(self, room_id: str, key: str, value: str) -> None:
        self.client.hset(self._room_key(room_id, "runtime-env"), key, value)

    def get_runtime_env(self, room_id: str) -> dict:
        return self.client.hgetall(self._room_key(room_id, "runtime-env"))

    def _approval_key(self, gate_id: str) -> str:
        return f"{self.prefix}:approval:{gate_id}"

    def _approval_votes_key(self, gate_id: str) -> str:
        return f"{self._approval_key(gate_id)}:votes"

    def create_approval(self, gate_id: str, room_id: str, requested_by: str,
                        action: dict, required: int, eligible_voters=None,
                        requested_quorum=None, self_approval_allowed=False) -> dict:
        gate = {"gate_id": gate_id, "room_id": room_id, "requested_by": requested_by,
                "action": action, "required": required, "votes": [], "status": "pending",
                "eligible_voters": list(eligible_voters or []),
                "requested_quorum": requested_quorum or required,
                "self_approval_allowed": bool(self_approval_allowed)}
        pipe = self.client.pipeline(transaction=True)
        pipe.sadd(self._room_key(room_id, "approvals"), gate_id)
        pipe.hset(self._approval_key(gate_id), mapping={
            "metadata": json.dumps({k: v for k, v in gate.items() if k not in ("votes", "status")}),
            "status": "pending",
        })
        pipe.expire(self._approval_key(gate_id), 86400)
        pipe.expire(self._approval_votes_key(gate_id), 86400)
        pipe.execute()
        return gate

    def list_approvals(self, room_id: str) -> List[dict]:
        gate_ids = self.client.smembers(self._room_key(room_id, "approvals"))
        gates = [self.get_approval(gate_id) for gate_id in gate_ids]
        return [gate for gate in gates if gate and gate.get("status") == "pending"]

    def get_approval(self, gate_id: str) -> Optional[dict]:
        values = self.client.hgetall(self._approval_key(gate_id))
        if not values:
            return None
        gate = json.loads(values["metadata"])
        gate["status"] = values.get("status", "pending")
        gate["votes"] = [json.loads(v) for v in self.client.hvals(self._approval_votes_key(gate_id))]
        return gate

    def cast_approval_vote(self, gate_id: str, voter_id: str, voter_name: str,
                           approve: bool, reason: str = "", active_voters=None) -> dict:
        gate_key, votes_key = self._approval_key(gate_id), self._approval_votes_key(gate_id)
        vote = json.dumps({"user_id": voter_id, "name": voter_name,
                           "approve": bool(approve), "reason": reason[:500]})
        script = """
        local meta = redis.call('HGET', KEYS[1], 'metadata')
        if not meta then return {'missing'} end
        local status = redis.call('HGET', KEYS[1], 'status')
        if status ~= 'pending' then return {'closed', status} end
        local request = cjson.decode(meta)
        local eligible = false
        for _, id in ipairs(request.eligible_voters or {}) do
          if id == ARGV[1] then eligible = true end
        end
        if not eligible then return {'ineligible'} end
        local active = cjson.decode(ARGV[3])
        local connected = false
        for _, id in ipairs(active) do
          if id == ARGV[1] then connected = true end
        end
        if not connected then return {'offline'} end
        if redis.call('HEXISTS', KEYS[2], ARGV[1]) == 1 then return {'duplicate'} end
        redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
        redis.call('EXPIRE', KEYS[2], 86400)
        local votes = redis.call('HVALS', KEYS[2])
        local yes, no = 0, 0
        for _, raw in ipairs(votes) do
          local item = cjson.decode(raw)
          if item.approve then yes = yes + 1 else no = no + 1 end
        end
        if no > 0 then status = 'rejected'
        elseif yes >= tonumber(request.required) then status = 'approved' end
        redis.call('HSET', KEYS[1], 'status', status)
        return {status}
        """
        result = self.client.eval(script, 2, gate_key, votes_key, voter_id, vote,
                                  json.dumps(list(active_voters or [])))
        if not result or result[0] in ("missing", "ineligible", "offline", "duplicate", "closed"):
            code = result[0] if result else "missing"
            messages = {"missing": "approval gate not found",
                        "ineligible": "you are not an eligible reviewer for this action",
                        "offline": "only connected reviewers can vote",
                        "duplicate": "this participant has already voted", "closed": "approval gate is already closed"}
            raise ValueError(messages.get(code, "approval vote was rejected"))
        gate = self.get_approval(gate_id)
        if gate is None:
            raise KeyError("approval gate not found")
        return gate

    def reconcile_approvals(self, room_id: str, active_voters) -> List[dict]:
        active_json = json.dumps(list(active_voters))
        changed = []
        for gate_id in self.client.smembers(self._room_key(room_id, "approvals")):
            gate_key, votes_key = self._approval_key(gate_id), self._approval_votes_key(gate_id)
            script = """
            local meta = redis.call('HGET', KEYS[1], 'metadata')
            if not meta or redis.call('HGET', KEYS[1], 'status') ~= 'pending' then return 0 end
            local request = cjson.decode(meta)
            local active = cjson.decode(ARGV[1])
            local voters = {}
            for _, id in ipairs(active) do voters[id] = true end
            local possible, yes = 0, 0
            for _, id in ipairs(request.eligible_voters or {}) do
              if voters[id] then possible = possible + 1 end
            end
            for _, raw in ipairs(redis.call('HVALS', KEYS[2])) do
              if cjson.decode(raw).approve then yes = yes + 1 end
            end
            local next_required = math.min(tonumber(request.required), math.max(1, possible, yes))
            if next_required == tonumber(request.required) then return 0 end
            request.required = next_required
            redis.call('HSET', KEYS[1], 'metadata', cjson.encode(request))
            if yes >= next_required then redis.call('HSET', KEYS[1], 'status', 'approved') end
            return 1
            """
            if self.client.eval(script, 2, gate_key, votes_key, active_json):
                gate = self.get_approval(gate_id)
                if gate:
                    changed.append(gate)
        return changed

    def expire_approval(self, gate_id: str) -> Optional[dict]:
        self.client.eval(
            "if redis.call('HGET', KEYS[1], 'status') == 'pending' then redis.call('HSET', KEYS[1], 'status', 'expired') end; return 1",
            1, self._approval_key(gate_id),
        )
        return self.get_approval(gate_id)
