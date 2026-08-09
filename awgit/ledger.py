"""Ledger attribution — the op-log as code contributions (Phase 6, ACTA seam).

The op-log is the code-contribution TWIN of the ARC world-model contribution
program (live: ``arc_register`` mints/exchanges ACTA wallets via the gateway;
the world-model leaderboard ranks accepted-transition contributions). Each
EditOp is a contribution entry: actor + verified_actor + git_sha + node_changes
+ timestamp. Attribution is EARNING, not GATING — an op NEVER blocks a commit;
it records who earned what, and a reward attaches to ``ledger_ref`` later.

``ledger_ref`` is the durable handle a reward can point at. It is minted
deterministically from (op_id, git_sha), so it is stable across op-log
replays/exports and needs no external counter. The entry shape mirrors ARC's
accepted-contribution record so the op-log can feed the same earning pipeline
without inventing a new economic model.

Phase 6 grounding note: the ACTA/ARC shape is "contributor identity → accepted
contribution → wallet credit". This module produces the identity+contribution
half; the wallet-credit half (actually moving ACTA) is the AitherLedger
integration that a future slice wires to the gateway.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, List

from awgit.schema import EditOp


@dataclass
class LedgerEntry:
    """One op as a ledger-contribution record (the ACTA/ARC shape)."""

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
    """Convert an op to the ledger entry (the ACTA/ARC contribution shape)."""
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
