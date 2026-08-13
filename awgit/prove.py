"""Proof-carrying review: what a change did, and what checked it.

A pull request normally arrives as a diff and an assertion. The reviewer's real
question — *has anything actually verified this?* — is answered by a green tick
whose meaning depends on which jobs ran, which were skipped, and which could not
run at all. "All checks passed" and "no checks ran" render almost identically.

``awgit prove`` attaches the evidence to the change itself: which nodes it
touched, and what each gate ACTUALLY returned.

**VIOLATION and DEAD are never merged.** A gate that exits 1 found something; a
gate that exits 2 could not judge. Collapsing them into "not ok" is how "the
checker is broken" gets filed as "the code is fine" — so they are counted
separately and reported separately, and a bundle with any DEAD gate is not
"proved", it is unproved with a reason.

The gate runner comes from the host through ``awgit.plugins``. The published
package cannot import a monorepo's gate suite, and hard-coding one would make
this feature useless to everyone else — a host registers ``plugins.GATES`` and
gets its own checks in the bundle.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

#: A gate's verdict. `dead` is deliberately not a kind of failure — it is the
#: absence of a verdict, which needs its own word or it becomes a pass.
OK, VIOLATION, DEAD = "ok", "violation", "dead"


@dataclass
class GateResult:
    name: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail[:400]}


@dataclass
class Bundle:
    change_id: str = ""
    sha: str = ""
    nodes: List[Dict[str, Any]] = field(default_factory=list)
    gates: List[GateResult] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    created_at: str = ""

    @property
    def violations(self) -> List[GateResult]:
        return [g for g in self.gates if g.status == VIOLATION]

    @property
    def dead(self) -> List[GateResult]:
        return [g for g in self.gates if g.status == DEAD]

    @property
    def proved(self) -> bool:
        """Only with at least one gate, no violations, and nothing DEAD.

        "No gates ran" is the case this whole module exists to stop reading as
        success — an empty gate list is the most common way a change arrives
        with a green tick and no evidence behind it.
        """
        return bool(self.gates) and not self.violations and not self.dead

    def to_dict(self) -> dict:
        return {
            "change_id": self.change_id, "sha": self.sha,
            "created_at": self.created_at, "proved": self.proved,
            "nodes": self.nodes,
            "gates": [g.to_dict() for g in self.gates],
            "counts": {"ok": len([g for g in self.gates if g.status == OK]),
                       "violation": len(self.violations), "dead": len(self.dead)},
            "notes": self.notes,
        }


def changed_nodes(sha: str = "HEAD", repo: Optional[Path] = None) -> List[Dict[str, Any]]:
    """The nodes this commit touched, from the op-log or computed."""
    from awgit.capture import capture_ops
    from awgit.oplog import OpLog

    try:
        log = OpLog()
        ops = log.ops_for_commit(sha)
        if not ops:
            op = capture_ops(sha)
            ops = [op] if op else []
    except Exception:  # noqa: BLE001 - evidence degrades, it does not fail
        return []
    out: List[Dict[str, Any]] = []
    for op in ops:
        for nc in op.node_changes:
            out.append({"node_id": nc.node_id, "symbol": nc.symbol,
                        "path": nc.path, "change": nc.change_type})
    return out


def run_gates(paths: List[str]) -> List[GateResult]:
    """Ask the host to run its gates. No host, no gates — and that is REPORTED."""
    from awgit import plugins

    raw = plugins.call(plugins.GATES, paths, default=None)
    if raw is None:
        return []
    results: List[GateResult] = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, GateResult):
            results.append(entry)
            continue
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status", "")).lower()
        if status not in (OK, VIOLATION, DEAD):
            # An unrecognised status must not become a pass. A host that
            # invents its own vocabulary gets DEAD, which is the honest reading
            # of "I do not know what this gate said".
            status = DEAD
        results.append(GateResult(name=str(entry.get("name", "?")),
                                  status=status, detail=str(entry.get("detail", ""))))
    return results


def build(sha: str = "HEAD", paths: Optional[List[str]] = None,
          repo: Optional[Path] = None) -> Bundle:
    from awgit.changeid import of_commit

    bundle = Bundle(
        change_id=of_commit(sha, repo) or "",
        sha=sha,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
    )
    bundle.nodes = changed_nodes(sha, repo)
    bundle.gates = run_gates(paths or [])
    if not bundle.gates:
        bundle.notes.append(
            "no gate runner is registered — nothing verified this change. "
            "A host wires one with awgit.plugins.register(plugins.GATES, fn).")
    if not bundle.change_id:
        bundle.notes.append(
            "this commit has no Change-Id, so the evidence cannot be tied to a "
            "pull request that survives an amend")
    return bundle


def render(bundle: Bundle) -> List[str]:
    lines = [f"  change {bundle.change_id[:13] or '(none)'}  {bundle.sha[:12]}",
             f"  nodes changed: {len(bundle.nodes)}"]
    for gate in bundle.gates:
        mark = {OK: "ok  ", VIOLATION: "FAIL", DEAD: "DEAD"}[gate.status]
        lines.append(f"    [{mark}] {gate.name}  {gate.detail[:60]}")
    lines.append(f"  verdict: {'PROVED' if bundle.proved else 'NOT PROVED'}")
    if bundle.dead:
        lines.append(f"    {len(bundle.dead)} gate(s) could NOT run — that is not "
                     f"a pass, it is an unanswered question")
    for note in bundle.notes:
        lines.append(f"  note: {note}")
    return lines


def to_markdown(bundle: Bundle) -> str:
    """The PR comment. Short enough to read, specific enough to act on."""
    head = "**Proved**" if bundle.proved else "**NOT proved**"
    rows = [f"{head} — {len(bundle.nodes)} node(s) changed", "",
            "| gate | result |", "|---|---|"]
    for gate in bundle.gates:
        icon = {OK: "pass", VIOLATION: "**FAIL**", DEAD: "**could not run**"}[gate.status]
        rows.append(f"| `{gate.name}` | {icon} |")
    if not bundle.gates:
        rows.append("| _(none registered)_ | nothing verified this change |")
    for note in bundle.notes:
        rows += ["", f"> {note}"]
    rows += ["", f"<sub>`awgit prove` · {bundle.created_at}Z</sub>"]
    return "\n".join(rows)


def to_json(bundle: Bundle) -> str:
    return json.dumps(bundle.to_dict(), indent=2)
