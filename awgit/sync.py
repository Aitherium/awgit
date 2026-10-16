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
from typing import Dict, List, Optional, Set, Tuple

from awgit.bodies import BodyStore, blob_sha
from awgit.data_root import vcs_data_root
from awgit.oplog import FileLock, OpLog
from awgit.schema import EditOp

_SYNC_SCHEMA = "awgit-sync-v1"


def _state_path(data_root: Path) -> Path:
    return data_root / "sync.json"


def _state_lock(data_root: Path) -> FileLock:
    # sync.json is read-modify-written. With vcs_sync_import reachable over the
    # network, two peers pushing at once would each drop the other's update.
    return FileLock(data_root / "sync.lock")


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
    with _state_lock(data):
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


def _json_len(value: object) -> int:
    return len(json.dumps(value, separators=(",", ":")))


def export_delta_page(
    known_op_ids: Set[str],
    max_bytes: int,
    *,
    data_root: Optional[Path] = None,
    include_bodies: bool = True,
) -> Tuple[Dict[str, object], int]:
    """The parent-first PREFIX of ``export_delta`` that fits ``max_bytes``.

    Returns ``(bundle, remaining)``. ``max_bytes`` bounds the base64 of the
    compact JSON, which is how the bundle travels. Ops are streamed in order
    and bodies are read only for the ops that make the page, so a page costs
    what it carries, not what the store holds. Building the full delta and then
    trimming it made every page of a clone re-read and re-encode everything
    still owed. The size is counted element by element against the same
    compact encoding. ``remaining`` > 0 with no ops means the next op alone is
    over the cap.
    """
    data = data_root or vcs_data_root()
    log = OpLog(data_root=data)
    ordered = log.linearize(
        [o.op_id for o in log.all_ops() if o.op_id not in known_op_ids])
    store = BodyStore(data_root=data) if include_bodies else None
    # base64 of n bytes is 4*ceil(n/3) chars, so it fits iff n <= 3*(cap//4).
    raw_cap = (max(0, int(max_bytes)) // 4) * 3
    # The skeleton with empty containers, minus its two one-digit counts; the
    # real counts' digits are added at each check.
    used = _json_len({"schema": _SYNC_SCHEMA, "op_count": 0, "body_count": 0,
                      "ops": [], "bodies": {}, "ledger_refs": []}) - 2
    ops_out: List[Dict[str, object]] = []
    bodies: Dict[str, str] = {}
    refs: List[str] = []
    for op in ordered:
        op_dict = op.to_dict()
        # A comma precedes every element but a container's first.
        cost = _json_len(op_dict) + (1 if ops_out else 0)
        new_bodies: Dict[str, str] = {}
        if store is not None:
            for nc in op.node_changes:
                for sha in (nc.old_body_sha, nc.new_body_sha):
                    if not sha or sha in bodies or sha in new_bodies:
                        continue
                    body = store.get(sha)
                    if body is not None:
                        b64 = base64.b64encode(body).decode("ascii")
                        cost += (1 if bodies or new_bodies else 0)
                        cost += _json_len(sha) + 1 + _json_len(b64)
                        new_bodies[sha] = b64
        if op.ledger_ref:
            cost += _json_len(op.ledger_ref) + (1 if refs else 0)
        digits = (len(str(len(ops_out) + 1))
                  + len(str(len(bodies) + len(new_bodies))))
        if used + cost + digits > raw_cap:
            break
        used += cost
        ops_out.append(op_dict)
        bodies.update(new_bodies)
        if op.ledger_ref:
            refs.append(op.ledger_ref)
    bundle: Dict[str, object] = {
        "schema": _SYNC_SCHEMA,
        "op_count": len(ops_out),
        "body_count": len(bodies),
        "ops": ops_out,
        "bodies": bodies,
        "ledger_refs": refs,
    }
    return bundle, len(ordered) - len(ops_out)


def import_delta(
    bundle: Dict[str, object], *, data_root: Optional[Path] = None
) -> Dict[str, object]:
    """Apply a delta bundle idempotently and advance the applied set.

    Idempotent: ops already applied (or whose commit is already present) are
    skipped, and bodies are content-addressed so re-putting is a no-op. A
    bundle applied twice converges to the same state — the "git pull/merge"
    semantics with no conflicts.

    ``skipped`` counts ops this node already holds, which is benign.
    ``rejected_ops`` counts ops it could NOT read (malformed, or a schema from a
    newer awgit) and so did not apply. A caller that ignores it reports a sync
    that did nothing as a success.
    """
    data = data_root or vcs_data_root()
    with _state_lock(data):
        return _import_locked(bundle, data)


def _import_locked(bundle: Dict[str, object], data: Path) -> Dict[str, object]:
    # The OpLog is loaded INSIDE the lock, so ops a concurrent import appended
    # are seen here and not appended again as duplicate lines.
    log = OpLog(data_root=data)
    store = BodyStore(data_root=data)
    applied = set(sync_state(data)["applied_op_ids"])

    imported_ops = 0
    skipped = 0
    rejected_ops = 0
    rejected_bodies = 0
    # materialize bodies first (idempotent by content address). The key IS the
    # claim: a body whose content does not hash to its key is refused, or a peer
    # (now reachable over the network) could make an op's sha resolve to other
    # bytes than the ones it names.
    for sha, b64 in bundle.get("bodies", {}).items():
        try:
            content = base64.b64decode(b64)
        except (ValueError, TypeError):
            rejected_bodies += 1
            continue
        if blob_sha(content) != sha:
            rejected_bodies += 1
            continue
        store.put(content)

    for op_dict in bundle.get("ops", []):
        try:
            op = EditOp.from_dict(op_dict)
        except (ValueError, KeyError, TypeError, AttributeError):
            rejected_ops += 1
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
        "rejected_ops": rejected_ops,
        "bodies": len(bundle.get("bodies", {})) - rejected_bodies,
        "rejected_bodies": rejected_bodies,
        "applied": len(applied),
    }
