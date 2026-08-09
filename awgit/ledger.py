"""Ledger attribution — the op-log as a durable who-changed-what record.

Each EditOp already carries everything attribution needs: actor, verified
GitHub identity, git_sha, node_changes, parent chain, timestamp. This module
flattens an op into a ``LedgerEntry`` — the record a team (or a downstream
reward program) can point at. The op-log itself only ever RECORDS; it never
gates a commit.

``ledger_ref`` is the durable attribution handle. It is minted deterministically
from (op_id, git_sha), so it is stable across op-log replays/exports and needs
no external counter.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, List

from awgit.schema import EditOp


@dataclass
class LedgerEntry:
    """One op flattened into an attribution record."""

    ledger_ref: str
    op_id: str
    actor: str
    verified_actor: str
    actor_verified: bool
    git_sha: str
    git_parent_sha: str
    ts: str
    file_paths: List[str]
    node_changes: int
    change_types: Dict[str, int]
    summary: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "ledger_ref": self.ledger_ref,
            "op_id": self.op_id,
            "actor": self.actor,
            "verified_actor": self.verified_actor,
            "actor_verified": self.actor_verified,
            "git_sha": self.git_sha,
            "git_parent_sha": self.git_parent_sha,
            "ts": self.ts,
            "file_paths": list(self.file_paths),
            "node_changes": self.node_changes,
            "change_types": dict(self.change_types),
            "summary": self.summary,
        }


def mint_ledger_ref(op_id: str, git_sha: str) -> str:
    """Deterministic ledger ref — stable across replays, no external state."""
    return hashlib.sha256(f"op:{op_id}:{git_sha}".encode()).hexdigest()[:16]


def op_to_ledger_entry(op: EditOp) -> LedgerEntry:
    """Convert an op to its attribution record."""
    counts: Dict[str, int] = {}
    for nc in op.node_changes:
        counts[nc.change_type] = counts.get(nc.change_type, 0) + 1
    return LedgerEntry(
        ledger_ref=op.ledger_ref or mint_ledger_ref(op.op_id, op.git_sha),
        op_id=op.op_id,
        actor=op.actor,
        verified_actor=op.verified_actor,
        actor_verified=op.actor_verified,
        git_sha=op.git_sha,
        git_parent_sha=op.git_parent_sha,
        ts=op.ts,
        file_paths=list(op.file_paths),
        node_changes=len(op.node_changes),
        change_types=counts,
        summary=op.summary,
    )
