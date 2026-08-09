"""Merge engine — node-granularity merge over op-sets.

Two op-sets A and B merge by touching NODE sets, not lines:

  - disjoint nodes -> clean by construction (no shared graph content)
  - shared node, identical bodies -> trivial merge (keep one)
  - shared node, line-compatible edits -> arbitrated: git's own 3-way merge is
    safe for disjoint hunks, so the engine APPROVES it (git does the bytes)
  - shared node, otherwise -> conflict: a ``MergeConflict`` naming the exact
    node, escalated to the human inbox (M5)

``MergeResult.conflicts`` is the escalation path. Blast radius via
``CodeGraph.impact_analysis`` is optional enrichment (``enrich=True``); the core
logic does not require the index, so ``merge_ops`` stays fast and testable.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional

from awgit.capture import default_repo, git_blob
from awgit.data_root import vcs_data_root
from awgit.diff import diff_opsets
from awgit.oplog import FileLock, OpLog
from awgit.schema import EditOp, MergeConflict, NodeChange

logger = logging.getLogger(__name__)


@dataclass
class MergeResult:
    status: str  # "clean" | "arbitrated" | "conflict"
    merged_node_ids: List[str] = field(default_factory=list)
    conflicts: List[MergeConflict] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


# ── git helpers ──────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    ).stdout.strip()


def _merge_base(repo: Path, a_ops: List[EditOp], b_ops: List[EditOp]) -> str:
    a_sha = max(a_ops, key=lambda o: o.ts).git_sha if a_ops else "HEAD"
    b_sha = max(b_ops, key=lambda o: o.ts).git_sha if b_ops else "HEAD"
    try:
        return _git(repo, "merge-base", a_sha, b_sha)
    except subprocess.CalledProcessError:
        return "HEAD"


def _op_sha(ops: List[EditOp], nc: NodeChange) -> str:
    for op in sorted(ops, key=lambda o: o.ts, reverse=True):
        if any(c.node_id == nc.node_id for c in op.node_changes):
            return op.git_sha
    return ops[0].git_sha


def _body_at(repo: Path, sha: str, path: str, symbol: str) -> Optional[str]:
    """Slice ``symbol``'s body out of the ``sha:path`` blob (None if absent)."""
    blob = git_blob(repo, sha, path)
    if blob is None:
        return None
    # lazy — see capture._node_records (CodeGraph import chain is ~15s)
    from awgit.parser import parse_source_bytes

    graph = parse_source_bytes(blob, path)
    lines = blob.decode("utf-8", errors="ignore").split("\n")
    for chunk in graph.chunks:
        if chunk.name == symbol and chunk.end_line:
            return "\n".join(lines[chunk.start_line - 1: chunk.end_line])
    return None


# ── arbitration primitives ───────────────────────────────────────────────

def _changed_ranges(base_body: str, other_body: str) -> List[tuple]:
    """Line ranges (in BASE coordinates) that ``other_body`` changed."""
    sm = SequenceMatcher(a=base_body.splitlines(), b=other_body.splitlines())
    ranges: List[tuple] = []
    for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
        if tag != "equal":
            ranges.append((i1, i2))
    return ranges


_MIN_MERGE_GAP = 2  # unchanged lines required between changed ranges


def _line_compatible(base_body, a_body, b_body) -> bool:
    """True if A's and B's changes merge cleanly under git's 3-way merge.

    Disjoint ranges are NOT enough: git merges hunks with ~3 lines of context,
    so ADJACENT changes (gap 0) conflict even though the lines don't overlap.
    Measured 2026-08-08 with a true 3-way merge sweep: gap 0 -> CONFLICT,
    gap >= 1 -> CLEAN. We require >= _MIN_MERGE_GAP unchanged lines for margin.
    """
    if base_body is None or a_body is None or b_body is None:
        return False
    ranges_a = _changed_ranges(base_body, a_body)
    ranges_b = _changed_ranges(base_body, b_body)
    if not ranges_a or not ranges_b:
        return False
    for r1 in ranges_a:
        for r2 in ranges_b:
            if not _ranges_separated(r1, r2):
                return False
    return True


def _ranges_separated(r1, r2, gap: int = _MIN_MERGE_GAP) -> bool:
    """True if [lo,hi) ranges are separated by >= ``gap`` unchanged lines."""
    lo1, hi1 = r1
    lo2, hi2 = r2
    if hi1 <= lo2:
        dist = lo2 - hi1
    elif hi2 <= lo1:
        dist = lo1 - hi2
    else:
        return False  # overlapping
    return dist >= gap


def _blast_radius(node_id: str, symbol: str, path: str) -> Dict[str, Any]:
    """Best-effort impact enrichment — never required, never fatal.

    Standalone awgit has no code-graph index, so there is no impact analysis
    to attach here — the conflict record already carries the bodies and the
    symbol, which is what the resolver needs. (The AitherOS monorepo's awgit
    enriches the same hook with its live CodeGraph before escalating.)
    """
    return {}


# ── conflict store ───────────────────────────────────────────────────────

def _conflicts_path(data_root: Path) -> Path:
    return data_root / "conflicts.jsonl"


def append_conflicts(data_root: Path, conflicts: List[MergeConflict]) -> None:
    """Append conflicts to the escalation log (append-only, fsync'd)."""
    data_root.mkdir(parents=True, exist_ok=True)
    with FileLock(data_root / "conflicts.lock"):
        with open(_conflicts_path(data_root), "a", encoding="utf-8") as f:
            for c in conflicts:
                f.write(json.dumps(c.to_dict()) + "\n")
                f.flush()
                os.fsync(f.fileno())


def list_conflicts(data_root: Optional[Path] = None) -> List[MergeConflict]:
    """Read the escalation log (all statuses)."""
    data = data_root or vcs_data_root()
    path = _conflicts_path(data)
    if not path.exists():
        return []
    out: List[MergeConflict] = []
    with FileLock(data / "conflicts.lock"):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                out.append(MergeConflict.from_dict(json.loads(line)))
    return out


# ── the engine ───────────────────────────────────────────────────────────

def merge_ops(
    a_ops: List[EditOp],
    b_ops: List[EditOp],
    *,
    repo_path: Optional[str] = None,
    data_root: Optional[Path] = None,
    enrich: bool = True,
) -> MergeResult:
    """Merge two op-sets at node granularity.

    Returns ``MergeResult``; ``status == "conflict"`` means a human must
    resolve (see ``conflicts``). Callers run the actual byte-merge (git) only
    for clean / arbitrated results.
    """
    repo = Path(repo_path or default_repo())
    data = data_root or vcs_data_root()
    overlap = diff_opsets(a_ops, b_ops)
    merged = overlap["only_a"] + overlap["only_b"]
    if not overlap["shared"]:
        return MergeResult(
            status="clean",
            merged_node_ids=merged,
            notes=["disjoint node sets — clean by construction"],
        )

    a_by_node = {nc.node_id: nc for op in a_ops for nc in op.node_changes}
    b_by_node = {nc.node_id: nc for op in b_ops for nc in op.node_changes}
    base_sha = _merge_base(repo, a_ops, b_ops)
    conflicts: List[MergeConflict] = []
    notes: List[str] = []
    arbitrated = 0
    for node_id in sorted(overlap["shared"]):
        a_nc, b_nc = a_by_node.get(node_id), b_by_node.get(node_id)
        if a_nc is None or b_nc is None:
            continue
        a_sha, b_sha = _op_sha(a_ops, a_nc), _op_sha(b_ops, b_nc)
        base_body = _body_at(repo, base_sha, a_nc.path, a_nc.symbol)
        a_body = _body_at(repo, a_sha, a_nc.path, a_nc.symbol)
        b_body = _body_at(repo, b_sha, b_nc.path, b_nc.symbol)
        if a_body is not None and a_body == b_body:
            notes.append(f"{node_id}: identical change — trivial merge")
            arbitrated += 1
            continue
        # M5 gate: a rename on EITHER side is high-risk (two agents renaming the
        # same node to different names must escalate, never silently merge).
        if "renamed" in (a_nc.change_type, b_nc.change_type):
            notes.append(f"{node_id}: rename involved — high risk, escalating")
            conflicts.append(_conflict(node_id, a_nc, base_body, a_body, b_body, enrich))
            continue
        if a_nc.change_type == "signature_changed" or b_nc.change_type == "signature_changed":
            notes.append(f"{node_id}: signature changed — high risk, escalating")
            conflicts.append(_conflict(node_id, a_nc, base_body, a_body, b_body, enrich))
            continue
        if _line_compatible(base_body, a_body, b_body):
            notes.append(f"{node_id}: line-compatible edits — git 3-way merge safe")
            arbitrated += 1
            continue
        conflicts.append(_conflict(node_id, a_nc, base_body, a_body, b_body, enrich))

    if conflicts:
        append_conflicts(data, conflicts)
        status = "conflict"
    elif arbitrated:
        status = "arbitrated"
    else:
        status = "clean"
    return MergeResult(
        status=status, merged_node_ids=merged, conflicts=conflicts, notes=notes
    )


def _conflict(
    node_id: str,
    nc: NodeChange,
    base_body: Optional[str],
    a_body: Optional[str],
    b_body: Optional[str],
    enrich: bool,
) -> MergeConflict:
    radius = _blast_radius(node_id, nc.symbol, nc.path) if enrich else {}
    return MergeConflict(
        conflict_id=uuid.uuid4().hex,
        node_id=node_id,
        symbol=nc.symbol,
        path=nc.path,
        base_body=base_body,
        a_body=a_body,
        b_body=b_body,
        blast_radius=radius,
        suggested=None,
        status="escalated",
    )


def resolve_conflict(
    conflict_id: str,
    *,
    resolved_body: str,
    resolver: str,
    data_root: Optional[Path] = None,
) -> Optional[EditOp]:
    """Mark a conflict resolved and record the resolution as a synthetic EditOp.

    The synthetic op carries ``actor="human:<resolver>"`` so the resolution is
    attributed to a real human in the op-log. Phase 6+ (AitherIdentity) makes
    this a verified identity rather than a self-asserted name.
    """
    data = data_root or vcs_data_root()
    conflicts = list_conflicts(data_root=data)
    target = next((c for c in conflicts if c.conflict_id == conflict_id), None)
    if target is None:
        return None
    target.status = "resolved"
    target.suggested = resolved_body
    _rewrite_conflicts(data, conflicts)
    op = EditOp(
        op_id=os.urandom(16).hex(),
        parent_ops=[],
        actor=f"human:{resolver}",
        ts=datetime.now(timezone.utc).isoformat(),
        git_sha="",
        git_parent_sha="",
        file_paths=[target.path] if target.path else [],
        node_changes=[
            NodeChange(
                node_id=target.node_id,
                change_type="body_rewrite",
                symbol=target.symbol,
                path=target.path,
                semantic_note=f"human resolution of conflict {conflict_id}",
            )
        ],
        summary=f"human:{resolver} resolved {conflict_id}",
        leased=False,
    )
    OpLog(data_root=data).append(op)
    return op


def _rewrite_conflicts(data_root: Path, conflicts: List[MergeConflict]) -> None:
    """Rewrite the escalation log with updated conflict statuses."""
    data_root.mkdir(parents=True, exist_ok=True)
    with FileLock(data_root / "conflicts.lock"):
        with open(_conflicts_path(data_root), "w", encoding="utf-8") as f:
            for c in conflicts:
                f.write(json.dumps(c.to_dict()) + "\n")
                f.flush()
                os.fsync(f.fileno())
