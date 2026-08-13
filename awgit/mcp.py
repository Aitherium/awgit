"""MCP tool surface for the semantic-VCS layer (gateway :8182).

Handlers are PURE functions returning JSON-able dicts — the tool LOGIC. Wiring
them into the gateway's MCP server is a thin seam (see ``register_tools``).
Phase 6+ integration targets (notebooks, RLM/AgentForge, AitherFlow) consume
exactly these handlers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from awgit.data_root import vcs_data_root
from awgit.diff import diff_git, render
from awgit.leases import LeaseConflictError, LeaseRegistry
from awgit.merge import list_conflicts
from awgit.oplog import OpLog


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


def vcs_lease_acquire(
    actor: str, targets: List[str], ttl_sec: int = 300, reason: str = "", **kw
) -> Dict[str, Any]:
    """Acquire leases all-or-nothing; returns the conflicting lease if any."""
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
    "vcs_lease_list": (vcs_lease_list, "List active edit leases"),
    "vcs_merge_conflicts": (vcs_merge_conflicts, "List escalated merge conflicts"),
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
