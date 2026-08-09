"""Semantic diff — 'what changed' at node granularity.

Renders the difference between two git shas (or two op-sets) as a list of
``NodeChange`` records, replacing raw unified diffs for review. The primitive
reuses capture's blob→nodes pipeline, so the ids shown are the same stable
node ids the op-log stores — the graph is the query plane over node history.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from awgit.capture import (
    default_repo,
    diff_sources,
    git_blob,
    git_changed_files,
    load_node_manager,
)
from awgit.data_root import vcs_data_root
from awgit.schema import EditOp, NodeChange


def diff_git(
    a_sha: str,
    b_sha: str,
    *,
    repo_path: Optional[str] = None,
    root_path: Optional[str] = None,
    data_root: Optional[Path] = None,
) -> List[NodeChange]:
    """Node-level diff between two git shas (Python files only)."""
    repo = Path(repo_path or default_repo())
    files = sorted(
        set(git_changed_files(repo, a_sha)) | set(git_changed_files(repo, b_sha))
    )
    data = data_root or vcs_data_root(root_path=root_path)
    manager = load_node_manager(data)
    changes: List[NodeChange] = []
    for rel in files:
        if not rel.endswith(".py"):
            continue
        changes.extend(
            diff_sources(
                git_blob(repo, a_sha, rel),
                git_blob(repo, b_sha, rel),
                rel,
                manager,
            )
        )
    return changes


def diff_opsets(a_ops: List[EditOp], b_ops: List[EditOp]) -> Dict[str, List[str]]:
    """Overlap summary of two op-sets — the merge engine's collision primitive."""
    a = {nc.node_id for op in a_ops for nc in op.node_changes}
    b = {nc.node_id for op in b_ops for nc in op.node_changes}
    return {
        "only_a": sorted(a - b),
        "only_b": sorted(b - a),
        "shared": sorted(a & b),
    }


def render(changes: List[NodeChange]) -> List[str]:
    """Human/agent-readable lines for a node-level diff."""
    out: List[str] = []
    for c in changes:
        loc = c.path
        if c.symbol:
            loc = f"{c.path}:{c.symbol}"
        if c.change_type == "body_rewrite":
            detail = f"body {c.old_body_sha[:8] or '?'} -> {c.new_body_sha[:8] or '?'}"
        elif c.change_type == "added":
            detail = "added"
        elif c.change_type == "deleted":
            detail = "deleted"
        else:
            detail = c.change_type
        out.append(f"[{c.change_type}] {loc} — {detail}")
    return out
