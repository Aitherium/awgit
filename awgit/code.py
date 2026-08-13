"""Where is this defined, and what else is named like it.

Scanning a working tree with grep answers "which lines contain this string".
awgit already has a node registry — every symbol capture has seen, with its
kind and its file — so the question can be answered against an index instead.

Deliberately narrow. This reports what the registry KNOWS, and says so when it
knows nothing: a symbol that has never been captured is absent, not missing,
and reporting "not found" for it would be a confident wrong answer. Call graphs
and reference lookup need a real index; a host with one registers it through
the plugin seam rather than having a half-version guessed at here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional


def definitions(symbol: str, data_root: Optional[Path] = None) -> List[Dict[str, str]]:
    """Every registered node whose name matches ``symbol`` exactly."""
    from awgit.capture import load_node_manager
    from awgit.data_root import vcs_data_root

    manager = load_node_manager(data_root or vcs_data_root())
    out: List[Dict[str, str]] = []
    for sid in manager.by_name(symbol) or []:
        node = manager.get_node(sid) or {}
        out.append({"node_id": sid, "name": node.get("name", symbol),
                    "path": node.get("path", ""), "type": node.get("type", "")})
    return out


def search(fragment: str, data_root: Optional[Path] = None,
           limit: int = 40) -> List[Dict[str, str]]:
    """Registered nodes whose name CONTAINS ``fragment`` (case-insensitive)."""
    from awgit.capture import load_node_manager
    from awgit.data_root import vcs_data_root

    manager = load_node_manager(data_root or vcs_data_root())
    needle = fragment.lower()
    out: List[Dict[str, str]] = []
    for sid, node in (manager.to_dict().get("nodes") or {}).items():
        if needle in str(node.get("name", "")).lower():
            out.append({"node_id": sid, "name": node.get("name", ""),
                        "path": node.get("path", ""), "type": node.get("type", "")})
        if len(out) >= limit:
            break
    return sorted(out, key=lambda r: (r["path"], r["name"]))


def render(rows: List[Dict[str, str]], what: str) -> List[str]:
    if not rows:
        return [f"  no registered node matches {what!r}",
                "  (the registry only holds nodes CAPTURE has seen — an "
                "uncaptured symbol is absent, not missing)"]
    return [f"  {r['type']:<9} {r['name']:<34} {r['path']}" for r in rows]
