"""MCP tool surface for the semantic-VCS layer (gateway :8182).

Handlers are PURE functions returning JSON-able dicts — the tool LOGIC. Wiring
them into the gateway's MCP server is a thin seam (see ``register_tools``).
The one consumer today is that gateway mount (the ``vcs_*`` MCP tools); any
other surface should call these same handlers rather than re-derive them.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from awgit.data_root import vcs_data_root
from awgit.diff import diff_git, render
from awgit.leases import LeaseConflictError, LeaseRegistry
from awgit.merge import list_conflicts
from awgit.oplog import OpLog
from awgit.query import QUERY_TOOLS
from awgit.sync import export_delta_page, import_delta


def _root(data_root: Optional[str]) -> Path:
    return Path(data_root) if data_root else vcs_data_root()


def vcs_semantic_diff(a: str, b: str, **kw) -> Dict[str, Any]:
    """Node-level diff between two shas (JSON shape for MCP/agents)."""
    changes = diff_git(a, b, data_root=_root(kw.get("data_root")))
    return {
        "count": len(changes),
        "changes": [c.to_dict() for c in changes],
        "rendered": render(changes),
    }


def vcs_oplog_query(
    sha: Optional[str] = None,
    node_id: Optional[str] = None,
    actor: Optional[str] = None,
    limit: int = 50,
    **kw,
) -> Dict[str, Any]:
    """Query the op-log by commit / node / actor."""
    log = OpLog(data_root=_root(kw.get("data_root")))
    if sha:
        ops = log.ops_for_commit(sha)
    elif node_id:
        ops = log.ops_for_node(node_id)
    elif actor:
        ops = log.ops_by(actor)
    else:
        ops = log.all_ops()
    return {"count": len(ops), "ops": [op.to_dict() for op in ops[-limit:]]}


#: Upper bound on an MCP-granted lease. ``ttl_sec`` is caller-supplied, and an
#: uncapped value (``10**9``) is a lock nothing ever expires. Renewal is how a
#: long edit keeps its lease; a single call must not reserve a target forever.
MCP_MAX_TTL_SEC = 3600


def vcs_lease_acquire(
    actor: str, targets: List[str], ttl_sec: int = 300, reason: str = "", **kw
) -> Dict[str, Any]:
    """Acquire leases all-or-nothing; returns the conflicting lease if any."""
    try:
        ttl_sec = max(1, min(MCP_MAX_TTL_SEC, int(ttl_sec)))
    except (TypeError, ValueError):
        ttl_sec = 300
    registry = LeaseRegistry(data_root=_root(kw.get("data_root")))
    try:
        leases = registry.acquire(actor, targets, ttl_sec=ttl_sec, reason=reason)
        return {"granted": [lz.to_dict() for lz in leases], "conflict": None}
    except LeaseConflictError as exc:
        return {"granted": [], "conflict": exc.holder.to_dict()}


def vcs_lease_list(actor: Optional[str] = None, **kw) -> Dict[str, Any]:
    """List active leases, optionally filtered by actor."""
    leases = LeaseRegistry(data_root=_root(kw.get("data_root"))).active_leases()
    if actor:
        leases = [lz for lz in leases if lz.actor == actor]
    return {"count": len(leases), "leases": [lz.to_dict() for lz in leases]}


def _resolve_ids(registry: LeaseRegistry, actor: str,
                 lease_ids: Optional[List[str]],
                 targets: Optional[List[str]]) -> Tuple[List[str], List[str]]:
    """Lease ids for ``actor`` from ids and/or leased targets -> (ids, unresolved).

    Same contract as the CLI: a remote caller passing the PATH it acquired must
    release that lease, and an argument matching nothing comes back unresolved
    instead of becoming a silent "released 0".
    """
    mine = [lz for lz in registry.active_leases() if lz.actor == actor]
    by_id = {lz.lease_id for lz in mine}
    by_target = {str(lz.target).replace("\\", "/").strip("/"): lz.lease_id
                 for lz in mine}
    ids: List[str] = []
    unresolved: List[str] = []
    for lid in lease_ids or []:
        (ids if lid in by_id else unresolved).append(lid)
    for t in targets or []:
        key = str(t).replace("\\", "/").strip("/")
        if key in by_target:
            ids.append(by_target[key])
        else:
            unresolved.append(t)
    return ids, unresolved


def vcs_lease_heartbeat(
    actor: str, lease_ids: Optional[List[str]] = None,
    targets: Optional[List[str]] = None, **kw,
) -> Dict[str, Any]:
    """Refresh the actor's leases (by id and/or leased target)."""
    registry = LeaseRegistry(data_root=_root(kw.get("data_root")))
    ids, unresolved = _resolve_ids(registry, actor, lease_ids, targets)
    return {"refreshed": registry.heartbeat(actor, ids) if ids else 0,
            "unresolved": unresolved}


def vcs_lease_release(
    actor: str, lease_ids: Optional[List[str]] = None,
    targets: Optional[List[str]] = None, **kw,
) -> Dict[str, Any]:
    """Release the actor's leases (by id and/or leased target)."""
    registry = LeaseRegistry(data_root=_root(kw.get("data_root")))
    ids, unresolved = _resolve_ids(registry, actor, lease_ids, targets)
    return {"released": registry.release(actor, ids) if ids else 0,
            "unresolved": unresolved}


#: Ceiling on one sync bundle as it travels (base64 of the JSON). A delta is a
#: coordination message, not a backup: a larger one (an initial clone) is
#: PAGED -- ``complete: false`` -- and the peer asks again with the op ids it
#: now holds. Import refuses anything over the same cap.
SYNC_MAX_BUNDLE_BYTES = 8 * 1024 * 1024


def _encode_bundle(bundle: Dict[str, Any]) -> str:
    raw = json.dumps(bundle, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _op_body_shas(op: Dict[str, Any]) -> Set[str]:
    out: Set[str] = set()
    for nc in op.get("node_changes") or []:
        for key in ("old_body_sha", "new_body_sha"):
            if nc.get(key):
                out.add(nc[key])
    return out


def _bundle_prefix(bundle: Dict[str, Any], n_ops: int) -> Dict[str, Any]:
    """The first ``n_ops`` ops of ``bundle`` with exactly the bodies they use."""
    ops = list(bundle.get("ops") or [])[:n_ops]
    need: Set[str] = set()
    for op in ops:
        need |= _op_body_shas(op)
    bodies = {s: b for s, b in (bundle.get("bodies") or {}).items() if s in need}
    return dict(bundle, ops=ops, bodies=bodies, op_count=len(ops),
                body_count=len(bodies),
                ledger_refs=[o.get("ledger_ref") for o in ops if o.get("ledger_ref")])


def _page_bundle(bundle: Dict[str, Any], cap: int) -> Tuple[Dict[str, Any], bool]:
    """The longest parent-first PREFIX of ``bundle`` whose encoding fits ``cap``.

    A prefix, never a sample: ops are linearized parent-first, so a page that
    skipped an op would hand the peer a child without its parent. Binary search
    on the prefix length, each probe a real encode -- no size estimate to drift.
    """
    total = len(bundle.get("ops") or [])
    if len(_encode_bundle(bundle)) <= cap:
        return bundle, True
    lo, hi = 0, total  # invariant: prefix(lo) fits (0 always does), prefix(hi) does not
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if len(_encode_bundle(_bundle_prefix(bundle, mid))) <= cap:
            lo = mid
        else:
            hi = mid
    return _bundle_prefix(bundle, lo), False


def vcs_sync_export(
    known_op_ids: Optional[List[str]] = None, include_bodies: bool = True, **kw,
) -> Dict[str, Any]:
    """The op-log delta a peer is missing, as one base64 JSON bundle.

    ``known_op_ids`` is the peer's applied set (its ``sync_state``); empty =
    full clone. A delta over SYNC_MAX_BUNDLE_BYTES is paged: ``complete`` is
    false, ``remaining`` says how many ops are still owed, and the caller
    repeats with the ids it just imported.
    """
    known = {str(x) for x in (known_op_ids or [])}
    page, remaining = export_delta_page(
        known, SYNC_MAX_BUNDLE_BYTES, data_root=_root(kw.get("data_root")),
        include_bodies=bool(include_bodies))
    return _export_reply(page, remaining)


def _export_reply(page: Dict[str, Any], remaining: int) -> Dict[str, Any]:
    """A streamed page as the export tool returns it, re-checked against the cap."""
    encoded = _encode_bundle(page)
    if len(encoded) > SYNC_MAX_BUNDLE_BYTES and page["ops"]:
        # The streamed size count and the real encoding disagree. Trim with real
        # encodes rather than ship an oversized page; this costs one page, not
        # the whole store.
        total = len(page["ops"])
        page, _ = _page_bundle(page, SYNC_MAX_BUNDLE_BYTES)
        remaining += total - len(page["ops"])
        encoded = _encode_bundle(page)
    if remaining and not page["ops"]:
        # One op alone is over the cap. Refuse by name rather than ship it or
        # return an empty page that a pull loop would retry forever.
        return {"error": f"a single op exceeds the {SYNC_MAX_BUNDLE_BYTES}-byte "
                         "bundle cap; export with include_bodies=false",
                "op_count": 0, "complete": False, "remaining": remaining}
    return {"bundle": encoded, "bytes": len(encoded), "op_count": page["op_count"],
            "body_count": page["body_count"], "complete": remaining == 0,
            "remaining": remaining}


def decode_bundle(bundle: Any) -> Dict[str, Any]:
    """A sync bundle off the wire: base64 JSON, raw JSON text, or a dict.

    Raises ``ValueError`` on anything oversized, malformed, or not an
    ``awgit-sync-v1`` bundle -- an import must never half-apply garbage.
    """
    if isinstance(bundle, dict):
        # The MCP layer can hand a JSON object straight through. The cap still
        # applies, measured on the same compact encoding the wire form uses.
        try:
            size = len(json.dumps(bundle, separators=(",", ":")))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"bundle is not JSON-serialisable: {exc}") from exc
        if size > SYNC_MAX_BUNDLE_BYTES:
            raise ValueError(f"bundle is {size} bytes; the cap is "
                             f"{SYNC_MAX_BUNDLE_BYTES}")
        data: Any = bundle
    else:
        text = str(bundle or "").strip()
        if len(text) > SYNC_MAX_BUNDLE_BYTES:
            raise ValueError(f"bundle is {len(text)} bytes; the cap is "
                             f"{SYNC_MAX_BUNDLE_BYTES}")
        if not text.startswith("{"):
            try:
                text = base64.b64decode(text, validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError(f"bundle is neither JSON nor base64 JSON: {exc}") from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"bundle is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != "awgit-sync-v1":
        raise ValueError("not an awgit-sync-v1 bundle")
    if not isinstance(data.get("ops", []), list) or not isinstance(
            data.get("bodies", {}), dict):
        raise ValueError("bundle ops/bodies have the wrong shape")
    if not all(isinstance(op, dict) for op in data.get("ops", [])):
        raise ValueError("bundle ops must all be objects")
    if not all(isinstance(k, str) and isinstance(v, str)
               for k, v in data.get("bodies", {}).items()):
        raise ValueError("bundle bodies must map sha strings to base64 strings")
    return data


def vcs_sync_import(bundle: str, **kw) -> Dict[str, Any]:
    """Apply a peer's delta bundle (base64 JSON) idempotently."""
    try:
        data = decode_bundle(bundle)
    except ValueError as exc:
        return {"error": str(exc), "imported_ops": 0}
    return import_delta(data, data_root=_root(kw.get("data_root")))


def vcs_merge_conflicts(status: Optional[str] = None, **kw) -> Dict[str, Any]:
    """List escalated merge conflicts, optionally filtered by status."""
    conflicts = list_conflicts(data_root=_root(kw.get("data_root")))
    if status:
        conflicts = [c for c in conflicts if c.status == status]
    return {"count": len(conflicts), "conflicts": [c.to_dict() for c in conflicts]}


TOOLS = {
    "vcs_semantic_diff": (vcs_semantic_diff, "Node-level diff between two git shas"),
    "vcs_oplog_query": (vcs_oplog_query, "Query the op-log by commit / node / actor"),
    "vcs_lease_acquire": (vcs_lease_acquire, "Acquire edit leases (all-or-nothing)"),
    "vcs_lease_heartbeat": (vcs_lease_heartbeat,
                            "Refresh your edit leases (by lease id or leased path)"),
    "vcs_lease_release": (vcs_lease_release,
                          "Release your edit leases (by lease id or leased path)"),
    "vcs_lease_list": (vcs_lease_list, "List active edit leases"),
    "vcs_sync_export": (vcs_sync_export,
                        "Export the op-log delta a peer is missing (base64 JSON bundle)"),
    "vcs_sync_import": (vcs_sync_import,
                        "Import a peer's op-log delta bundle (idempotent)"),
    "vcs_merge_conflicts": (vcs_merge_conflicts, "List escalated merge conflicts"),
    # Read-only repository queries (blame, path log, merge base, file at a rev,
    # diff, merge preview): the repository is the server's, never an argument.
    **QUERY_TOOLS,
}


def register_tools(server) -> int:
    """Register the handlers on an MCP server; returns how many were ACTUALLY wired.

    This used to ``return 5`` and register nothing, with the real call spelled
    out in the docstring as a to-do. Every caller therefore read "5 tools
    registered" and got none — the silent-no-op pattern, in the one function
    whose entire job is to make a surface exist. Nothing could see it: the
    import worked, the count was right, and the tools were simply absent, which
    is indistinguishable from a client that never asked for them.

    Returns the real count, so a caller can compare it against ``len(TOOLS)``.
    Raises ``TypeError`` if ``server`` exposes no registration API at all: a
    server that cannot register is a wiring bug, and falling back to a cheerful
    zero would rebuild exactly the defect this replaces.
    """
    register = getattr(server, "tool", None) or getattr(server, "add_tool", None)
    if register is None:
        raise TypeError(
            f"{type(server).__name__} exposes neither .tool() nor .add_tool() — "
            f"cannot register awgit's {len(TOOLS)} MCP tools"
        )
    wired = 0
    for name, (fn, desc) in TOOLS.items():
        try:
            decorated = register(name, desc)
            # .tool(name, desc) may return a decorator, or register directly.
            if callable(decorated):
                decorated(fn)
            wired += 1
        except Exception as exc:  # noqa: BLE001 - one bad tool must not lose the rest
            import logging

            logging.getLogger(__name__).warning(
                "awgit.mcp: could not register %s (%s)", name, exc
            )
    return wired
