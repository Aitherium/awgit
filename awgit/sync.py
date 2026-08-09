"""Differential sync — the op-log + body store as the mesh teleport unit.

The semantic twin of git's push/pull for the AitherNet mesh: instead of copying
whole trees, nodes exchange the DIFFERENTIAL — ops (what changed, in order) +
bodies (the content, content-addressed + deduped).

  - ``export_delta(known_op_ids)`` — the ops a peer is missing + the bodies
    they reference. Empty known set = a full initial clone; a caught-up peer
    gets a tiny delta.
  - ``import_delta(bundle)`` — apply idempotently (ops keyed by op_id/git_sha,
    bodies by content address) and advance the applied set.
  - ``sync_state`` / ``sync_status`` — what a node has, persisted in sync.json.

TRANSPORT (the Homa connection): the bundle is a portable JSON artifact — any
AitherNet endpoint carries it over the mesh overlay. **Homa (AF_HOMA) is the
critical fabric-phase transport** for the incast-heavy pattern this sync
produces: many nodes pulling MISSING bodies by content address from the few
holding them is exactly Homa's many-to-one incast. Tunnel legs (public
internet) fall back to TCP/HTTPS and must log loudly. ``--meta-only`` emits
ops WITHOUT bodies so a node learns the state shape and pulls bodies
on-demand by sha — the pull-by-sha pattern that keeps deltas tiny and Homa
efficient. Kernel module PlatformLab/HomaModule, NOT the userspace DPDK repo.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Dict, Optional, Set

from awgit.bodies import BodyStore
from awgit.data_root import vcs_data_root
from awgit.oplog import OpLog
from awgit.schema import EditOp

_SYNC_SCHEMA = "awgit-sync-v1"


def _state_path(data_root: Path) -> Path:
    return data_root / "sync.json"


def sync_state(data_root: Optional[Path] = None) -> Dict[str, object]:
    """The node's applied op-ids (persisted)."""
    data = data_root or vcs_data_root()
    path = _state_path(data)
    if not path.exists():
        return {"applied_op_ids": []}
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        return {"applied_op_ids": list(rec.get("applied_op_ids", []))}
    except (OSError, ValueError):
        return {"applied_op_ids": []}


def _save_state(data_root: Path, op_ids: Set[str]) -> None:
    data_root.mkdir(parents=True, exist_ok=True)
    tmp = _state_path(data_root).with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"applied_op_ids": sorted(op_ids)}), encoding="utf-8"
    )
    os.replace(tmp, _state_path(data_root))


def mark_op_applied(op_id: str, data_root: Optional[Path] = None) -> None:
    """Record an op as applied — self-captured or imported. Idempotent."""
    data = data_root or vcs_data_root()
    applied = set(sync_state(data)["applied_op_ids"])
    if op_id in applied:
        return
    applied.add(op_id)
    _save_state(data, applied)


def sync_status(data_root: Optional[Path] = None) -> Dict[str, object]:
    """What this node has vs what it has applied (the sync health)."""
    data = data_root or vcs_data_root()
    ops = OpLog(data_root=data).all_ops()
    bodies = BodyStore(data_root=data).stats()
    applied = set(sync_state(data)["applied_op_ids"])
    missing = [o.op_id for o in ops if o.op_id not in applied]
    return {
        "ops": len(ops),
        "applied": len(applied),
        "missing": len(missing),
        "bodies": bodies["blobs"],
        "bytes": bodies["bytes"],
    }


def export_delta(
    known_op_ids: Set[str],
    *,
    data_root: Optional[Path] = None,
    include_bodies: bool = True,
) -> Dict[str, object]:
    """The ops a peer is missing + the bodies they reference (the differential).

    ``known_op_ids`` is the peer's applied set (from its ``sync_state``); empty
    = a full initial clone. Ops are linearized parent-first so an import
    preserves causality. Bodies are content-addressed and deduped — a body the
    peer already hosts is not re-sent. ``include_bodies=False`` emits ops only
    (the state shape) so the peer can pull bodies on-demand by sha.
    """
    data = data_root or vcs_data_root()
    log = OpLog(data_root=data)
    all_ops = log.all_ops()
    new_ops = [o for o in all_ops if o.op_id not in known_op_ids]
    ordered = log.linearize([o.op_id for o in new_ops]) if new_ops else []
    ops_out = [op.to_dict() for op in ordered]  # linearize returns EditOp objects

    bodies: Dict[str, str] = {}
    if include_bodies and new_ops:
        store = BodyStore(data_root=data)
        referenced: Set[str] = set()
        for op in new_ops:
            for nc in op.node_changes:
                for s in (nc.old_body_sha, nc.new_body_sha):
                    if s:
                        referenced.add(s)
        for sha in referenced:
            body = store.get(sha)
            if body is not None:
                bodies[sha] = base64.b64encode(body).decode("ascii")
    return {
        "schema": _SYNC_SCHEMA,
        "op_count": len(ops_out),
        "body_count": len(bodies),
        "ops": ops_out,
        "bodies": bodies,
        "ledger_refs": [o.ledger_ref for o in new_ops if o.ledger_ref],
    }


def import_delta(
    bundle: Dict[str, object], *, data_root: Optional[Path] = None
) -> Dict[str, object]:
    """Apply a delta bundle idempotently and advance the applied set.

    Idempotent: ops already applied (or whose commit is already present) are
    skipped, and bodies are content-addressed so re-putting is a no-op. A
    bundle applied twice converges to the same state — the "git pull/merge"
    semantics with no conflicts.
    """
    data = data_root or vcs_data_root()
    log = OpLog(data_root=data)
    store = BodyStore(data_root=data)
    applied = set(sync_state(data)["applied_op_ids"])

    imported_ops = 0
    skipped = 0
    # materialize bodies first (idempotent by content address)
    for sha, b64 in bundle.get("bodies", {}).items():
        store.put(base64.b64decode(b64))

    for op_dict in bundle.get("ops", []):
        try:
            op = EditOp.from_dict(op_dict)
        except (ValueError, KeyError):
            skipped += 1
            continue
        if op.op_id in applied:
            skipped += 1
            continue
        if op.git_sha and log.has_commit(op.git_sha):
            skipped += 1
            applied.add(op.op_id)  # already here via another path
            continue
        log.append(op)
        applied.add(op.op_id)
        imported_ops += 1
    _save_state(data, applied)
    return {
        "imported_ops": imported_ops,
        "skipped": skipped,
        "bodies": len(bundle.get("bodies", {})),
        "applied": len(applied),
    }
