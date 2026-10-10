"""Snapshot hashing — makes snapshots content-addressed and dedup-friendly."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from shared.protocols.python.protocols import Snapshot


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON bytes: sorted keys, no whitespace, UTF-8.

    This is the canonical form used to compute content hashes so that two
    snapshots with identical logical content always hash the same, regardless
    of dict insertion order.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def compute_content_hash(snapshot: Snapshot) -> str:
    """SHA-256 hex over the snapshot's logical fields (excluding identity/time).

    ``snapshot_id`` and ``created_at`` are intentionally excluded: a snapshot's
    *content* is what's hashed, not its metadata.
    """
    body = asdict(snapshot)
    body.pop("snapshot_id", None)
    body.pop("created_at", None)
    body["content_hash"] = ""  # ignore whatever was set
    return hashlib.sha256(canonical_json(body)).hexdigest()


def stamp(snapshot: Snapshot) -> Snapshot:
    """Fill ``content_hash`` (and default ``node_id`` link) in place."""
    snapshot.content_hash = compute_content_hash(snapshot)
    return snapshot
