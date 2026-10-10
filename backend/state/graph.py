"""Versioned execution state graph.

Model
-----
A *node* is one committed step of the agent. Its ``parents`` list (length >= 1)
points back through the graph:

    * 1 parent  -> a linear step (or a branch off a prior node)
    * 2 parents -> a merge of two branches (DAG join)

``head_id`` is the active tip. Branching moves the head to a new alternative
node sharing the same parent; merging joins two tips. Everything is a DAG, so
a node's *ancestors* are well-defined and time-travel = reading any ancestor's
pinned snapshot.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from shared.protocols.python.protocols import GraphEdge, GraphNode, Snapshot
from .snapshot import stamp


class GraphError(Exception):
    """Raised for invalid graph operations (unknown node, cycle, bad merge)."""


class StateGraph:
    def __init__(self, room_id: str = ""):
        self.room_id = room_id
        self.nodes: Dict[str, GraphNode] = {}
        self.snapshots: Dict[str, Snapshot] = {}
        self.root_id: Optional[str] = None
        self.head_id: Optional[str] = None

    # -- access ---------------------------------------------------------
    def node(self, node_id: str) -> GraphNode:
        try:
            return self.nodes[node_id]
        except KeyError:
            raise GraphError(f"unknown node: {node_id}") from None

    def snapshot_for(self, node_id: str) -> Snapshot:
        node = self.node(node_id)
        try:
            return self.snapshots[node.snapshot_id]
        except KeyError:
            raise GraphError(f"node {node_id} has no snapshot {node.snapshot_id}") from None

    @property
    def is_empty(self) -> bool:
        return not self.nodes

    # -- construction ---------------------------------------------------
    def add_root(self, label: str, snapshot: Snapshot) -> GraphNode:
        if self.root_id is not None:
            raise GraphError("graph already has a root")
        snapshot = stamp(snapshot)
        self._store_snapshot(snapshot)
        node = GraphNode(
            node_id=snapshot.snapshot_id[:16],  # stable, readable
            room_id=self.room_id,
            label=label,
            parents=[],
            step_index=0,
            kind="root",
            snapshot_id=snapshot.snapshot_id,
        )
        self.nodes[node.node_id] = node
        self.root_id = node.node_id
        self.head_id = node.node_id
        return node

    def add_step(self, label: str, snapshot: Snapshot, kind: str = "step") -> GraphNode:
        """Append a step continuing from the current head (or seed the root)."""
        if self.head_id is None:
            return self.add_root(label, snapshot)
        parent = self.node(self.head_id)
        return self._add_node(
            label=label,
            parents=[parent.node_id],
            step_index=parent.step_index + 1,
            kind=kind,
            snapshot=snapshot,
        )

    def branch(self, from_node_id: str, label: str, snapshot: Snapshot) -> GraphNode:
        """Fork a new hypothesis-testing branch from an existing node.

        The new node reuses ``from_node_id`` as its parent and becomes the new
        head, so subsequent steps continue along the fork. The original branch
        remains reachable in the DAG (it is not destroyed).
        """
        origin = self.node(from_node_id)  # raises GraphError if unknown
        node = self._add_node(
            label=label,
            parents=[origin.node_id],
            step_index=origin.step_index + 1,
            kind="branch",
            snapshot=snapshot,
        )
        self.head_id = node.node_id
        return node

    def merge(self, a_node_id: str, b_node_id: str, label: str,
              snapshot: Snapshot) -> GraphNode:
        """Merge two nodes into one (DAG join). Rejects self-merge and cycles."""
        if a_node_id == b_node_id:
            raise GraphError("cannot merge a node with itself")
        a, b = self.node(a_node_id), self.node(b_node_id)
        if self._is_ancestor(a_node_id, b_node_id) or self._is_ancestor(b_node_id, a_node_id):
            raise GraphError("cannot merge a node with its own ancestor (would cycle)")
        node = self._add_node(
            label=label,
            parents=[a_node_id, b_node_id],
            step_index=max(a.step_index, b.step_index) + 1,
            kind="merge",
            snapshot=snapshot,
        )
        self.head_id = node.node_id
        return node

    def _add_node(self, label: str, parents: List[str], step_index: int,
                  kind: str, snapshot: Snapshot) -> GraphNode:
        snapshot = stamp(snapshot)
        self._store_snapshot(snapshot)
        node = GraphNode(
            node_id=snapshot.snapshot_id[:16],
            room_id=self.room_id,
            label=label,
            parents=list(parents),
            step_index=step_index,
            kind=kind,
            snapshot_id=snapshot.snapshot_id,
        )
        if node.node_id in self.nodes:
            # identical content re-derived — reuse the existing node id space
            node.node_id = f"{node.node_id}-{len(self.nodes):04d}"
        self.nodes[node.node_id] = node
        return node

    def _store_snapshot(self, snapshot: Snapshot) -> None:
        snapshot.node_id = snapshot.snapshot_id[:16]
        self.snapshots[snapshot.snapshot_id] = snapshot

    # -- traversal ------------------------------------------------------
    def ancestors(self, node_id: str) -> List[str]:
        """All transitive ancestors (parents first), excluding the node itself."""
        out: List[str] = []
        seen = set()
        stack = [node_id]
        while stack:
            cur = stack.pop()
            for p in self.nodes.get(cur, GraphNode(node_id=cur)).parents:
                if p not in seen and p != node_id:
                    seen.add(p)
                    out.append(p)
                    stack.append(p)
        return out

    def descendants(self, node_id: str) -> List[str]:
        out: List[str] = []
        for nid, n in self.nodes.items():
            if nid != node_id and node_id in n.parents:
                out.append(nid)
        # expand transitively
        expanded: List[str] = []
        changed = True
        frontier = list(out)
        while changed:
            changed = False
            for nid, n in self.nodes.items():
                if nid not in expanded and any(p in out for p in n.parents):
                    expanded.append(nid)
                    changed = True
        out = list(dict.fromkeys(out + expanded))
        return out

    def _is_ancestor(self, maybe_ancestor: str, node: str) -> bool:
        return maybe_ancestor in self.ancestors(node)

    def timeline(self) -> List[GraphNode]:
        """Default timeline for the scrubber: head -> root along the primary
        (first) parent, returned root->head. Deterministic even with merges."""
        if self.head_id is None:
            return []
        path: List[GraphNode] = []
        cur = self.head_id
        guard = 0
        while cur is not None and guard < len(self.nodes) + 1:
            node = self.nodes.get(cur)
            if node is None:
                break
            path.append(node)
            cur = node.parents[0] if node.parents else None
            guard += 1
        path.reverse()
        return path

    def edges(self) -> List[GraphEdge]:
        out: List[GraphEdge] = []
        for n in self.nodes.values():
            for p in n.parents:
                out.append(GraphEdge(src=p, dst=n.node_id, weight=1.0))
        return out

    # -- serialization --------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "room_id": self.room_id,
            "root_id": self.root_id,
            "head_id": self.head_id,
            "nodes": [self._node_to_dict(n) for n in self.nodes.values()],
            "edges": [self._edge_to_dict(e) for e in self.edges()],
            "snapshots": {sid: self._snapshot_to_dict(s) for sid, s in self.snapshots.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StateGraph":
        g = cls(room_id=d.get("room_id", ""))
        g.root_id = d.get("root_id")
        g.head_id = d.get("head_id")
        for nd in d.get("nodes", []):
            g.nodes[nd["node_id"]] = GraphNode(**_pick(GraphNode, nd))
        for sid, s in (d.get("snapshots") or {}).items():
            g.snapshots[sid] = _snapshot_from_dict(s)
        return g

    # -- dict adapters (tolerant of extra/missing keys) -----------------
    @staticmethod
    def _node_to_dict(n: GraphNode) -> dict:
        return {
            "node_id": n.node_id, "room_id": n.room_id, "label": n.label,
            "parents": list(n.parents), "step_index": n.step_index, "kind": n.kind,
            "snapshot_id": n.snapshot_id, "created_at": n.created_at, "meta": n.meta,
        }

    @staticmethod
    def _edge_to_dict(e: GraphEdge) -> dict:
        return {"src": e.src, "dst": e.dst, "weight": e.weight}

    @staticmethod
    def _snapshot_to_dict(s: Snapshot) -> dict:
        from dataclasses import asdict
        from shared.protocols.python.protocols import MemoryEntry  # noqa: F401
        return asdict(s)


def _pick(node_cls, d: dict) -> dict:
    """Return only the fields present on the dataclass (forward-compat)."""
    import dataclasses
    fields = {f.name for f in dataclasses.fields(node_cls)}
    return {k: v for k, v in d.items() if k in fields}


def _snapshot_from_dict(d: dict) -> Snapshot:
    import dataclasses
    from shared.protocols.python.protocols import MemoryEntry, RunState

    mem = [MemoryEntry(kind=m["kind"], text=m["text"]) for m in d.get("memory", [])]
    fields = {f.name for f in dataclasses.fields(Snapshot)}
    base = {k: v for k, v in d.items() if k in fields and k != "memory"}
    base["memory"] = mem
    if isinstance(base.get("run_state"), str):
        base["run_state"] = RunState(base["run_state"])
    return Snapshot(**base)
