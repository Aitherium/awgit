#!/usr/bin/env python3
"""Generate awgit's `aither-manifest.json` — the ecosystem contract's reference impl.

awgit is the first node in the network to publish live metrics, and it is the
honest test of the contract: the numbers come from `awgit evidence`, which reads
the op-log this tool itself produced.

THE RULE THIS SCRIPT EXISTS TO OBEY: absent beats stale. If the evidence cannot
be gathered, this writes NOTHING and exits non-zero, leaving the previous file
alone for CI to fail on — rather than emitting a manifest with placeholder or
last-known values. The hub renders a missing manifest as `unknown`, which is
true; it cannot tell a stale file from a fresh one, so a stale file becomes a
lie that outlives whoever wrote it.

    python scripts/gen_manifest.py --out docs/aither-manifest.json
    python scripts/gen_manifest.py --self-test
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "1"


def build(evidence: dict, version: str, now: str) -> dict:
    """Assemble the manifest. Pure, so the self-test needs no op-log."""
    metrics = {}
    if evidence:
        # Only the numbers that mean something to an outsider. `ops` alone is
        # vanity; the collision count is the claim.
        metrics = {
            "ops captured": {"value": evidence.get("ops", 0), "as_of": now},
            "actors": {"value": evidence.get("actor_count", 0), "as_of": now},
            "confirmed multi-agent collisions": {
                "value": evidence.get("confirmed_multi_agent_collisions", 0),
                "as_of": now,
            },
            "lease adoption": {
                "value": evidence.get("lease_adoption_pct", 0),
                "unit": "%", "as_of": now,
            },
        }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "name": "awgit",
        "description": (
            "Semantic version control on top of git: edits are keyed to stable "
            "code-node ids, so two agents editing the same function is visible "
            "instead of silent."
        ),
        "url": "https://aitherium.github.io/awgit/",
        "repository": "https://github.com/Aitherium/awgit",
        "kind": "tool",
        "language": "Python",
        "license": "Apache-2.0",
        "install": {"pip": "pip install awgit"},
        "tags": ["vcs", "agents", "semantic-diff", "multi-agent"],
        "metrics": metrics,
        "links": {"docs": "https://aitherium.github.io/awgit/"},
        "version": version,
        "generated_at": now,
    }


def _package_version() -> str:
    for parent in (Path(__file__).resolve().parents[1],):
        pj = parent / "pyproject.toml"
        if pj.is_file():
            for line in pj.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("version"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def self_test() -> int:
    """Prove the shape holds and that missing evidence yields NO metrics."""
    now = "2026-08-10T00:00:00Z"
    failures = []

    full = build({"ops": 209, "actor_count": 8,
                  "confirmed_multi_agent_collisions": 1,
                  "lease_adoption_pct": 1.0}, "0.3.0", now)
    for req in ("schemaVersion", "name", "description", "url"):
        if not full.get(req):
            failures.append(f"required field missing: {req}")
    if full["metrics"]["actors"]["value"] != 8:
        failures.append("metrics not carried through")
    for label, m in full["metrics"].items():
        if "as_of" not in m:
            failures.append(f"metric {label!r} has no as_of — undated numbers "
                            f"cannot be aged and must not ship")

    # No evidence -> NO metrics block. Never zeros, which would read as a
    # measured 'nothing happened' rather than 'not measured'.
    empty = build({}, "0.3.0", now)
    if empty["metrics"] != {}:
        failures.append("absent evidence must yield NO metrics, got "
                        f"{empty['metrics']}")
    if not empty.get("description"):
        failures.append("static fields must survive absent evidence")

    if failures:
        print("SELF-TEST FAIL:")
        for f in failures:
            print("  - " + f)
        return 1
    print("self-test ok: shape holds, and absent evidence yields no metrics")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="docs/aither-manifest.json")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    try:
        from awgit.evidence import gather

        evidence = gather()
    except Exception as exc:
        # ABSENT BEATS STALE. Fail loudly; write nothing.
        print(f"manifest: could not gather evidence ({type(exc).__name__}) — "
              f"writing NOTHING rather than a stale or placeholder manifest",
              file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = build(evidence, _package_version(), now)
    out = Path(args.out)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"manifest: cannot write {out}: {exc}", file=sys.stderr)
        return 2
    print(f"manifest: wrote {out} "
          f"({manifest['metrics'].get('ops captured', {}).get('value', 0)} ops, "
          f"{manifest['metrics'].get('actors', {}).get('value', 0)} actors)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
