"""
memory.py — Stateful memory graph for agents (Sapien/LangGraph-inspired).

Every reasoning step appends a Node to a versioned graph. Nodes form DAG
branches via `parent` links; `branch` identifies the hypothesis thread.
A snapshot is a (branch, node_id) pair — time-travel inspection is just
reading the stored node, never recomputation.

Graph-based state versioning pillar:
  - append:   store.append()           new node on top of current branch
  - branch:   store.create_branch()    new hypothesis lane
  - scrub:    store.get_node(node_id)  exact stored state at that time
  - fork:     store.fork_from(node_id) new branch inheriting history
  - inspect:  store.branch_state(b)    full replay payload at branch head
  - policies: store.policies           Sapien-style per-branch guardrails
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from ..state import gen_id


class MemoryStore:
    """In-process versioned memory graph for one room's agent team."""

    def __init__(self) -> None:
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.branches: Dict[str, Dict[str, Any]] = {}
        self.policies: Dict[str, Dict[str, Any]] = {}
        self.current_branch: str = "main"
        self._init_main()

    # -- branch registry ----------------------------------------------------
    def _init_main(self) -> None:
        self.branches["main"] = {
            "name": "main", "head": None, "parents": [],
            "created": time.time(), "merges": [],
        }
        self.policies["main"] = {
            "denied_tools": [], "min_approvals": 0, "max_tool_calls": 12,
            "note": "default lane - no forced gates",
        }

    def create_branch(self, name: str, parent_branch: Optional[str] = None) -> str:
        parent_branch = parent_branch or self.current_branch
        self.branches[name] = {
            "name": name,
            "head": self.branches.get(parent_branch, {}).get("head"),
            "parents": [parent_branch] if parent_branch != name else [],
            "created": time.time(), "merges": [],
        }
        self.policies[name] = {
            "denied_tools": [], "min_approvals": 2, "max_tool_calls": 8,
            "note": "hypothesis lane - gated by default",
        }
        return name

    # -- append / fork -------------------------------------------------------
    def append(self, agent_id: str, agent_name: str, content: str,
               kind: str = "thought", tool: Optional[str] = None,
               args: Optional[Dict[str, Any]] = None,
               steering: Optional[List[str]] = None) -> Dict[str, Any]:
        branch = self.current_branch
        head = self.branches.get(branch, {}).get("head")
        node = {
            "id": gen_id("node"),
            "branch": branch,
            "parent": head,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "kind": kind,  # thought | tool_call | tool_result | steering | reply
            "content": content,
            "tool": tool,
            "args": args,
            "steering_applied": steering or [],
            "ts": time.time(),
            "seq": len(self.nodes),
        }
        self.nodes[node["id"]] = node
        self.branches.setdefault(branch, {}).update({"head": node["id"]})
        return node

    def fork_from(self, node_id: str, new_branch: str) -> str:
        """Fork a new branch based at an existing node (time-travel replay)."""
        if node_id not in self.nodes:
            raise ValueError("unknown node_id: %r" % node_id)
        if new_branch in self.branches:
            raise ValueError("branch %r already exists" % new_branch)
        self.current_branch = self.branch_of(node_id) or self.current_branch
        return self.create_branch(new_branch)

    def set_branch(self, branch: str) -> None:
        if branch not in self.branches:
            raise ValueError("unknown branch: %r" % branch)
        self.current_branch = branch

    # -- inspection -----------------------------------------------------------
    def branch_of(self, node_id: str) -> Optional[str]:
        n = self.nodes.get(node_id)
        return n["branch"] if n else None

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        return self.nodes.get(node_id)

    def path_to(self, node_id: Optional[str]) -> List[Dict[str, Any]]:
        if node_id is None:
            node_id = (self.branches.get(self.current_branch) or {}).get("head")
        out: List[Dict[str, Any]] = []
        cur = node_id
        seen = set()
        while cur and cur in self.nodes and cur not in seen:
            seen.add(cur)
            out.append(self.nodes[cur])
            cur = self.nodes[cur]["parent"]
        out.reverse()
        return out

    def branch_state(self, branch: str) -> Dict[str, Any]:
        head = (self.branches.get(branch) or {}).get("head")
        return {
            "branch": branch,
            "head": head,
            "nodes": self.path_to(head) if head else [],
            "policies": self.policies.get(branch, {}),
        }

    def all_branches(self) -> List[Dict[str, Any]]:
        out = []
        for name, meta in self.branches.items():
            out.append({
                "name": name,
                "head": meta["head"],
                "depth": len(self.path_to(meta["head"])) if meta["head"] else 0,
                "current": name == self.current_branch,
                "policies": self.policies.get(name, {}),
                "merges": meta["merges"],
            })
        return out

    # -- policies (Sapien-style per-branch guardrails) -------------------------
    def allow_tool(self, branch: str, tool: str) -> Tuple[bool, str]:
        pol = self.policies.get(branch, {})
        if tool in (pol.get("denied_tools") or []):
            return False, "denied by branch policy"
        return True, ""

    def approvals_required(self, branch: str) -> int:
        return int((self.policies.get(branch, {}) or {}).get("min_approvals", 0))

    def stats(self) -> Dict[str, Any]:
        return {
            "nodes": len(self.nodes),
            "branches": len(self.branches),
            "current_branch": self.current_branch,
            "branch_names": list(self.branches.keys()),
        }
