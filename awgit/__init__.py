"""AitherOS semantic version-control layer — git + a world model.

Rides ON TOP of git (git stays the byte-transport + history). Adds: edit-ops
keyed on stable graph node ids, a durable op-log, semantic diff, a
node-granularity merge engine, and lease-based conflict prevention.

M1–M4 exports (schema / oplog / capture / diff / leases / merge). MCP surface
lands with the merge engine in M4–M5.
"""

from __future__ import annotations

from awgit.acta import (
    contribution_record,
    mark_submitted,
    submit_contribution,
    was_submitted,
)
from awgit.bodies import BodyStore, blob_sha, dedupe_report, reclaim, scan_tree
from awgit.capture import capture_ops
from awgit.data_root import vcs_data_root
from awgit.diff import diff_git, diff_opsets, render
from awgit.identity import acta_user_id, github_email, provision_acta_user
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
    "acta_user_id",
    "blob_sha",
    "capture_ops",
    "contribution_record",
    "dedupe_report",
    "diff_git",
    "diff_opsets",
    "export_delta",
    "github_email",
    "import_delta",
    "list_conflicts",
    "mark_submitted",
    "merge_ops",
    "mint_ledger_ref",
    "op_to_ledger_entry",
    "provision_acta_user",
    "reclaim",
    "render",
    "resolve_conflict",
    "scan_tree",
    "submit_contribution",
    "sync_state",
    "sync_status",
    "vcs_data_root",
    "was_submitted",
]
