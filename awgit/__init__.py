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


def _installed_version() -> str:
    """Version from installed metadata; "" when awgit is not an installed dist."""
    try:
        from importlib.metadata import version

        return version("awgit")
    except Exception:  # noqa: BLE001 - absence is a normal state, see _resolve_version
        return ""


def _pyproject_version() -> str:
    """Version from the adjacent pyproject; "" when there is no source checkout."""
    import re
    from pathlib import Path

    try:
        text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
            encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    return match.group(1) if match else ""


def _resolve_version() -> str:
    """The version, DERIVED — never a literal that can drift from pyproject.

    The adjacent pyproject wins when there IS one, because that means we are
    running from a source checkout and the source is what someone is editing.
    Installed metadata is the answer for anyone who pip-installed us.

    That order is deliberate and was measured the moment it was written the
    other way round: with metadata first this returned **0.1.0** while
    pyproject said 0.3.1, because an editable install records the version it
    was installed AT and never refreshes. Reporting a version two releases
    stale — from the tool whose own release rail exists because it once sat at
    0.1.0 for a month while the package kept moving — is worse than reporting
    nothing.

    A hardcoded ``__version__`` is not an option either: it is a second source
    of truth for the one string a release is identified by, and the drift only
    becomes visible once something is published under the wrong number.

    Each source RETURNS "" when it does not apply rather than swallowing the
    error, so "not installed" and "no checkout" are ordinary answers here, not
    suppressed failures. The last resort is a marker nobody can mistake for a
    real release.
    """
    return _pyproject_version() or _installed_version() or "0+unknown"


__version__ = _resolve_version()

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
