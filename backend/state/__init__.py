"""State graph, versioning, and persistence.

Public surface:
    StateGraph  — in-memory versioned execution graph (branch/merge/scrub)
    StateStore  — SQLite persistence backend for graphs
    stamp       — content-address a Snapshot
"""

from .snapshot import compute_content_hash, canonical_json, stamp
from .graph import StateGraph
from .store import StateStore

__all__ = ["StateGraph", "StateStore", "compute_content_hash", "canonical_json", "stamp"]
