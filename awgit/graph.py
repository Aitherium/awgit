"""Render the op-log as a GRAPH — awgit's own data, drawn.

awgit is already a graph and was only ever printed as lines. Every edit op is
an edge: an ACTOR touched a NODE (a function or class with a stable id) that
lives in a FILE, at a commit. Rendering that answers, at a glance, the question
the whole lease plane exists for — *who is working where, and where are two
actors standing on the same node* — instead of making someone reconstruct it
from `awgit ledger` output.

Two shapes, deliberately:

- ``mermaid`` for humans: files as subgraphs, code nodes inside them, actors as
  rounded nodes with edges into what they touched. A node TWO OR MORE actors
  touched is drawn as a collision, because that is the only part of the picture
  that means anything is wrong.
- ``json`` for machines: plain nodes/edges so the platform's graph plane can
  ingest it. awgit's graph is one graph among many — code, services, memory —
  and this is the seam that lets it be a subgraph of that larger graph rather
  than a private format nothing else can read.

Deliberately NO layout, colours-by-actor or clustering heuristics: this renders
what the op-log says. If the picture is wrong, the op-log is wrong, and that is
worth seeing rather than smoothing over.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Optional

from awgit.oplog import OpLog


def _short(text: str, keep: int = 28) -> str:
    text = text or "?"
    return text if len(text) <= keep else text[:keep - 1] + "…"


def build(data_root=None, since: Optional[str] = None, actor: Optional[str] = None) -> dict:
    """Collect the op-log into {nodes, edges, collisions}. Pure data, no rendering."""
    log = OpLog(data_root=data_root)
    ops = log.ops_since(since) if since else log.all_ops()
    if actor:
        ops = [o for o in ops if getattr(o, "actor", None) == actor]

    files: dict = defaultdict(set)      # path -> {(node_id, symbol)}
    touched: dict = defaultdict(set)    # (path, node_id) -> {actor}
    actors: set = set()
    for op in ops:
        who = getattr(op, "actor", None) or "unknown"
        actors.add(who)
        for ch in (getattr(op, "node_changes", None) or []):
            nid = getattr(ch, "node_id", None)
            if not nid:
                continue
            path = getattr(ch, "path", None) or "?"
            files[path].add((nid, getattr(ch, "symbol", None) or nid[:12]))
            touched[(path, nid)].add(who)

    collisions = sorted(k for k, v in touched.items() if len(v) > 1)
    return {
        "ops": len(ops),
        "actors": sorted(actors),
        "files": {p: sorted(v) for p, v in files.items()},
        "touched": {f"{p}::{n}": sorted(v) for (p, n), v in touched.items()},
        "collisions": [{"path": p, "node_id": n, "actors": sorted(touched[(p, n)])}
                       for p, n in collisions],
    }


def to_mermaid(g: dict) -> str:
    """A mermaid `graph LR` of actors -> nodes, grouped by file."""
    out = ["graph LR"]
    if not g["files"]:
        # An empty graph must SAY it is empty. A bare `graph LR` renders as a
        # blank box, which reads as "nothing is wrong" rather than "no data".
        out.append('  empty["op-log is empty — no captures yet"]')
        return "\n".join(out)

    ids: dict = {}
    for fi, (path, nodes) in enumerate(sorted(g["files"].items())):
        out.append(f'  subgraph F{fi}["{_short(path, 40)}"]')
        for ni, (nid, symbol) in enumerate(nodes):
            key = f"N{fi}_{ni}"
            ids[(path, nid)] = key
            hits = g["touched"].get(f"{path}::{nid}", [])
            marker = "💥 " if len(hits) > 1 else ""
            out.append(f'    {key}["{marker}{_short(symbol)}"]')
        out.append("  end")

    for ai, who in enumerate(g["actors"]):
        out.append(f'  A{ai}("{_short(who, 34)}")')

    seen = set()
    for key, whos in g["touched"].items():
        path, _, nid = key.rpartition("::")
        target = ids.get((path, nid))
        if not target:
            continue
        for who in whos:
            ai = g["actors"].index(who)
            edge = (ai, target)
            if edge in seen:
                continue
            seen.add(edge)
            out.append(f"  A{ai} --> {target}")

    for c in g["collisions"]:
        target = ids.get((c["path"], c["node_id"]))
        if target:
            out.append(f"  style {target} fill:#a33,stroke:#f66,color:#fff")
    return "\n".join(out)


def to_json(g: dict) -> str:
    """Node/edge form for ingestion into the platform's graph-of-graphs."""
    nodes = [{"id": f"actor:{a}", "kind": "actor", "label": a} for a in g["actors"]]
    edges = []
    for path, entries in g["files"].items():
        nodes.append({"id": f"file:{path}", "kind": "file", "label": path})
        for nid, symbol in entries:
            nodes.append({"id": f"node:{path}::{nid}", "kind": "code_node",
                          "label": symbol, "path": path})
            edges.append({"src": f"node:{path}::{nid}", "dst": f"file:{path}",
                          "rel": "in_file"})
    for key, whos in g["touched"].items():
        for who in whos:
            edges.append({"src": f"actor:{who}", "dst": f"node:{key}",
                          "rel": "touched"})
    return json.dumps({
        "graph": "awgit.oplog", "ops": g["ops"],
        "nodes": nodes, "edges": edges, "collisions": g["collisions"],
    }, indent=2)
