"""awgit — Aither World-Graph git: semantic version control on top of git.

Rides ON TOP of git (git stays the byte-transport + history). Adds: edit-ops
keyed on stable graph node ids, a durable op-log, semantic diff, a
node-granularity merge engine, lease-based conflict prevention, content-addressed
bodies, verified-identity attribution, and differential sync.
"""

from __future__ import annotations

from awgit.bodies import BodyStore, blob_sha, dedupe_report, reclaim, scan_tree
from awgit.capture import capture_ops
from awgit.data_root import vcs_data_root
from awgit.diff import diff_git, diff_opsets, render
from awgit.identity import attribution_id, github_email
from awgit.leases import LeaseRegistry
from awgit.ledger import LedgerEntry, mint_ledger_ref, op_to_ledger_entry
from awgit.merge import MergeResult, list_conflicts, merge_ops, resolve_conflict
from awgit.oplog import OpLog
from awgit.schema import SCHEMA_VERSION, EditOp, MergeConflict, NodeChange
from awgit.sync import export_delta, import_delta, sync_state, sync_status

__all__ = [
    "BodyStore",
    "EditOp",
    "LedgerEntry",
    "MergeConflict",
    "MergeResult",
    "NodeChange",
    "SCHEMA_VERSION",
    "OpLog",
    "LeaseRegistry",
    "attribution_id",
    "blob_sha",
    "capture_ops",
    "dedupe_report",
    "diff_git",
    "diff_opsets",
    "export_delta",
    "github_email",
    "import_delta",
    "list_conflicts",
    "merge_ops",
    "mint_ledger_ref",
    "op_to_ledger_entry",
    "reclaim",
    "render",
    "resolve_conflict",
    "scan_tree",
    "sync_state",
    "sync_status",
    "vcs_data_root",
]
