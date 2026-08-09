"""Edit-op schema — the contract every semantic-VCS component shares.

SCHEMA_VERSION gates ``from_dict``: a record written by a NEWER version is
refused loudly rather than silently misparsed (a future record is a hard
error, never a guess).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

CHANGE_TYPES = (
    "body_rewrite",
    "added",
    "deleted",
    "renamed",
    "moved",
    "signature_changed",
)


@dataclass
class NodeChange:
    """One node (function/class/method) changed by an edit-op."""

    node_id: str
    change_type: str
    old_body_sha: Optional[str] = None
    new_body_sha: Optional[str] = None
    symbol: str = ""
    path: str = ""
    renamed_from: Optional[str] = None
    moved_from: Optional[str] = None
    semantic_note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "change_type": self.change_type,
            "old_body_sha": self.old_body_sha,
            "new_body_sha": self.new_body_sha,
            "symbol": self.symbol,
            "path": self.path,
            "renamed_from": self.renamed_from,
            "moved_from": self.moved_from,
            "semantic_note": self.semantic_note,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "NodeChange":
        return cls(
            node_id=d["node_id"],
            change_type=d["change_type"],
            old_body_sha=d.get("old_body_sha"),
            new_body_sha=d.get("new_body_sha"),
            symbol=d.get("symbol", ""),
            path=d.get("path", ""),
            renamed_from=d.get("renamed_from"),
            moved_from=d.get("moved_from"),
            semantic_note=d.get("semantic_note", ""),
        )


@dataclass
class EditOp:
    """One commit's semantic annotation."""

    op_id: str
    parent_ops: List[str]
    actor: str
    ts: str
    git_sha: str
    git_parent_sha: str
    file_paths: List[str]
    node_changes: List[NodeChange]
    summary: str = ""
    leased: bool = False
    schema_version: int = SCHEMA_VERSION
    # Actor provenance (added additively — old ops parse with these defaults).
    # `actor` is the CLAIMED attribution (explicit arg / AITHER_ACTOR / git
    # author); `actor_verified` + `verified_actor` record the box's VERIFIED
    # identity (the Aitherium GitHub OAuth app / GitHub App login via `gh`)
    # INDEPENDENTLY, so a session claiming `AITHER_ACTOR=lyra` on a box verified
    # as `wizzense` records both. The verified half is the authoritative
    # attribution; self-asserted actor is forgeable and the op-log now says so
    # explicitly.
    actor_verified: bool = False
    actor_source: str = "env"
    verified_actor: str = ""
    # Durable attribution handle — a stable id for this op that replays/exports
    # can point at. Minted deterministically from (op_id, git_sha) — see
    # ledger.py. The op-log only RECORDS; it never gates a commit.
    ledger_ref: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op_id": self.op_id,
            "parent_ops": list(self.parent_ops),
            "actor": self.actor,
            "ts": self.ts,
            "git_sha": self.git_sha,
            "git_parent_sha": self.git_parent_sha,
            "file_paths": list(self.file_paths),
            "node_changes": [nc.to_dict() for nc in self.node_changes],
            "summary": self.summary,
            "leased": self.leased,
            "schema_version": self.schema_version,
            "actor_verified": self.actor_verified,
            "actor_source": self.actor_source,
            "verified_actor": self.verified_actor,
            "ledger_ref": self.ledger_ref,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EditOp":
        sv = d.get("schema_version", 0)
        if not isinstance(sv, int) or sv > SCHEMA_VERSION:
            raise ValueError(
                f"EditOp schema_version {sv!r} is newer than supported "
                f"{SCHEMA_VERSION} — refusing to misparse. Rebuild the reader "
                "or upgrade the exporter."
            )
        for key in ("op_id", "git_sha", "git_parent_sha"):
            if key not in d:
                raise ValueError(f"EditOp missing required field {key!r}")
        return cls(
            op_id=d["op_id"],
            parent_ops=list(d.get("parent_ops", [])),
            actor=d.get("actor", "unknown"),
            ts=d.get("ts", ""),
            git_sha=d["git_sha"],
            git_parent_sha=d["git_parent_sha"],
            file_paths=list(d.get("file_paths", [])),
            node_changes=[NodeChange.from_dict(x) for x in d.get("node_changes", [])],
            summary=d.get("summary", ""),
            leased=bool(d.get("leased", False)),
            schema_version=sv,
            actor_verified=bool(d.get("actor_verified", False)),
            actor_source=d.get("actor_source", "env"),
            verified_actor=d.get("verified_actor", ""),
            ledger_ref=d.get("ledger_ref", ""),
        )


@dataclass
class MergeConflict:
    """A collision the merge engine could not resolve and escalated."""

    conflict_id: str
    node_id: str
    symbol: str
    path: str = ""
    base_body: Optional[str] = None
    a_body: Optional[str] = None
    b_body: Optional[str] = None
    blast_radius: Dict[str, Any] = field(default_factory=dict)
    suggested: Optional[str] = None
    status: str = "escalated"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "node_id": self.node_id,
            "symbol": self.symbol,
            "path": self.path,
            "base_body": self.base_body,
            "a_body": self.a_body,
            "b_body": self.b_body,
            "blast_radius": self.blast_radius,
            "suggested": self.suggested,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MergeConflict":
        return cls(
            conflict_id=d["conflict_id"],
            node_id=d["node_id"],
            symbol=d.get("symbol", ""),
            path=d.get("path", ""),
            base_body=d.get("base_body"),
            a_body=d.get("a_body"),
            b_body=d.get("b_body"),
            blast_radius=d.get("blast_radius", {}),
            suggested=d.get("suggested"),
            status=d.get("status", "escalated"),
        )
