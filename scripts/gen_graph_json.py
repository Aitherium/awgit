#!/usr/bin/env python3
"""Emit a RENDERABLE slice of the op-log graph for awgit's own page.

The full graph is thousands of nodes — correct, and unreadable as a picture. A
hairball communicates "lots of stuff" and nothing else, which is worse than no
diagram. So this emits a slice chosen to show the thing that matters: every
COLLISION (a node two sessions touched), the files those live in, and enough of
the busiest files around them for scale.

Counts in the payload are always the FULL numbers, never the slice's. A viewer
must not read "14 files" off a picture that was truncated for legibility.

    python scripts/gen_graph_json.py --out docs/graph.json
    python scripts/gen_graph_json.py --self-test
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MAX_FILES = 14
MAX_NODES_PER_FILE = 10


def slice_graph(g: dict, max_files: int = MAX_FILES,
                max_nodes: int = MAX_NODES_PER_FILE) -> dict:
    """Pick a legible subgraph. Pure — the self-test drives it with a fixture."""
    files = g.get("files", {})
    touched = g.get("touched", {})
    collisions = g.get("collisions", [])
    actors = g.get("actors", [])

    collision_keys = {(c["path"], c["node_id"]) for c in collisions}
    collision_files = {c["path"] for c in collisions}

    # Collision files first — they are the whole point — then the busiest.
    ordered = sorted(collision_files) + [
        p for p, _ in sorted(files.items(), key=lambda kv: -len(kv[1]))
        if p not in collision_files
    ]
    chosen = ordered[:max_files]

    out_nodes, out_edges = [], []
    for path in chosen:
        entries = files.get(path, [])
        # Always keep the colliding nodes in a file, then fill with others.
        hot = [e for e in entries if (path, e[0]) in collision_keys]
        rest = [e for e in entries if (path, e[0]) not in collision_keys]
        keep = (hot + rest)[:max_nodes]
        out_nodes.append({"id": f"f:{path}", "kind": "file",
                          "label": path.split("/")[-1], "path": path})
        for nid, symbol in keep:
            who = touched.get(f"{path}::{nid}", [])
            out_nodes.append({
                "id": f"n:{path}::{nid}", "kind": "node",
                "label": (symbol or nid)[:34],
                "actors": len(who),
                "collision": (path, nid) in collision_keys,
            })
            out_edges.append({"s": f"n:{path}::{nid}", "t": f"f:{path}"})

    return {
        "sliced": True,
        "shown": {"files": len([n for n in out_nodes if n["kind"] == "file"]),
                  "nodes": len([n for n in out_nodes if n["kind"] == "node"])},
        # FULL counts — the picture is a sample, the numbers are not.
        "totals": {"files": len(files), "nodes": len(touched),
                   "actors": len(actors), "collisions": len(collisions)},
        "nodes": out_nodes,
        "edges": out_edges,
    }


def self_test() -> int:
    g = {
        "files": {"a/x.py": [("n1", "alpha"), ("n2", "beta")],
                  "b/y.ts": [("n3", "gamma")],
                  "c/z.py": [("n4", "delta")]},
        "touched": {"a/x.py::n1": ["s1", "s2"], "a/x.py::n2": ["s1"],
                    "b/y.ts::n3": ["s1"], "c/z.py::n4": ["s2"]},
        "collisions": [{"path": "a/x.py", "node_id": "n1",
                        "actors": ["s1", "s2"]}],
        "actors": ["s1", "s2"],
    }
    fails = []
    s = slice_graph(g, max_files=2, max_nodes=5)

    if s["totals"]["files"] != 3:
        fails.append("totals must be the FULL count, not the slice's")
    if s["shown"]["files"] > 2:
        fails.append("slice exceeded max_files")
    colliding = [n for n in s["nodes"] if n.get("collision")]
    if len(colliding) != 1:
        fails.append(f"the colliding node must survive slicing, got {colliding}")
    if not any(n["id"] == "f:a/x.py" for n in s["nodes"]):
        fails.append("the collision's FILE must be included")
    # A tiny slice must still keep the collision rather than the busiest file.
    s2 = slice_graph(g, max_files=1, max_nodes=5)
    if not any(n.get("collision") for n in s2["nodes"]):
        fails.append("collision dropped when the slice is tight — the one thing "
                     "that must never be truncated away")
    if slice_graph({}, 5, 5)["nodes"]:
        fails.append("an empty graph must yield no nodes")

    if fails:
        print("SELF-TEST FAIL:")
        for f in fails:
            print("  - " + f)
        return 1
    print("self-test ok: collisions survive slicing, totals stay full")
    return 0


def main() -> int:
    # Anchored to the package root, not CWD — a sibling generator in this
    # same directory (gen_manifest.py) had this exact CWD-relative default
    # and it silently wrote a stray file when run from the repo root,
    # leaving the real docs/graph.json untouched with git diff genuinely
    # empty. Fix applied here before this one ever shipped the same bug.
    default_out = Path(__file__).resolve().parents[1] / "docs" / "graph.json"
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(default_out))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    try:
        from awgit.graph import build

        g = build()
    except Exception as exc:
        print(f"graph: could not build ({type(exc).__name__}) — writing nothing",
              file=sys.stderr)
        return 2
    payload = slice_graph(g)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload), encoding="utf-8")
    print(f"graph: wrote {out} — {payload['shown']['nodes']} nodes shown of "
          f"{payload['totals']['nodes']}, {payload['totals']['collisions']} collisions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
