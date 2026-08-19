"""Durable append-only outcome log for gate verification results.

An ``Outcome`` records which gates verified a commit and what they said,
durable in the outcomes.jsonl file alongside the oplog. Like the oplog it is
append-only JSONL with an OS-level exclusive lock + fsync per append.

The key property: "unknown" (no outcomes recorded) is distinct from "passed"
(outcomes recorded, all ok). Conflating these would poison training signals:
a commit nobody checked would appear identical to one that passed all checks.

Outcomes are NOT queryable by git_sha uniqueness — multiple runs of prove on
the same commit create multiple outcome records. That is deliberate: a commit
proved once at a point in time, then checked again later with a new gate,
should show both outcomes. A consumer that needs "the most recent outcome"
reads all of them and picks the last.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from awgit.data_root import vcs_data_root
from awgit.oplog import FileLock, _lock_path
from awgit.schema import Outcome

_HEADER = "# outcomes-log v1"


class OutcomeLog:
    """Append-only outcome log backed by one JSONL file."""

    def __init__(self, data_root: Optional[Path] = None) -> None:
        self._data_root = data_root or vcs_data_root()
        self.path = self._data_root / "outcomes.jsonl"
        self._outcomes: List[Outcome] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with FileLock(_lock_path(self._data_root)):
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    outcome = Outcome.from_dict(json.loads(line))
                    self._outcomes.append(outcome)

    def append(self, outcome: Outcome) -> None:
        """Append one outcome durably. Idempotent by outcome_id."""
        existing = [o.outcome_id for o in self._outcomes]
        if outcome.outcome_id in existing:
            return
        with FileLock(_lock_path(self._data_root)):
            self._data_root.mkdir(parents=True, exist_ok=True)
            fresh = not self.path.exists()
            with open(self.path, "a", encoding="utf-8") as f:
                if fresh:
                    f.write(_HEADER + "\n")
                f.write(json.dumps(outcome.to_dict()) + "\n")
                f.flush()
                os.fsync(f.fileno())
        self._outcomes.append(outcome)

    def all_outcomes(self) -> List[Outcome]:
        return list(self._outcomes)

    def outcomes_for_commit(self, git_sha: str) -> List[Outcome]:
        return [o for o in self._outcomes if o.git_sha == git_sha]

    def latest_outcome(self, git_sha: str) -> Optional[Outcome]:
        """The most recent outcome for a commit, or None if never checked."""
        outcomes = self.outcomes_for_commit(git_sha)
        if not outcomes:
            return None
        return outcomes[-1]


def record_outcome(
    git_sha: str,
    gates: List,
    data_root: Optional[Path] = None,
) -> Outcome:
    """Create and durably record an outcome for a commit.

    Args:
        git_sha: the commit being verified
        gates: list of GateResult objects from prove.run_gates()
        data_root: optional override of the vcs data root

    Returns:
        the recorded Outcome
    """
    from awgit.schema import OutcomeGate

    # IDENTITY IS THE CONTENT, NOT THE CLOCK.
    #
    # This was `f"{git_sha[:16]}-{int(time.time())}"` — one-second resolution —
    # and `OutcomeLog.append` is idempotent by outcome_id. So two outcomes for
    # the same commit inside the same second collapsed to one, and the loser was
    # dropped SILENTLY: append returns normally and record_outcome hands back an
    # Outcome that was never persisted, so no caller can tell.
    #
    # It bit immediately. A sweep re-proving a commit recorded `unknown` moments
    # earlier discarded the REAL verdict and kept the unknown — turning the
    # retry path this module had just gained back into a no-op, invisibly.
    #
    # Hashing the verdict-bearing content fixes both directions at once:
    # re-proving with the SAME result is genuinely idempotent (one row, which is
    # what idempotence should mean here), while re-proving and getting a
    # DIFFERENT result is a different outcome and gets its own row — which is
    # exactly right, because "it failed, then it passed" is history worth having.
    _fingerprint = hashlib.blake2b(
        json.dumps(
            [git_sha, sorted((g.name, g.status) for g in gates)],
            sort_keys=True,
        ).encode("utf-8"),
        digest_size=8,
    ).hexdigest()
    outcome_id = f"{git_sha[:16]}-{_fingerprint}"
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

    gate_records = [
        OutcomeGate(name=g.name, status=g.status, detail=g.detail)
        for g in gates
    ]

    outcome = Outcome(
        outcome_id=outcome_id,
        git_sha=git_sha,
        ts=ts,
        gates=gate_records,
    )

    log = OutcomeLog(data_root=data_root)
    log.append(outcome)
    return outcome


def sweep_unproven(
    limit: int = 20,
    data_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Attach outcomes to captured ops that have none. Returns metrics.

    WHY A SWEEP AND NOT THE POST-COMMIT HOOK.

    Recording the outcome where the op is captured is the obvious design and it
    is the wrong one: the host's gates run 30-180s, and a hook that adds minutes
    to every commit gets disabled rather than fixed — which would leave the
    outcome log empty for a reason nobody could see. So capture stays fast and
    unconditional, and verification catches up out of band.

    That split is only safe because "not checked yet" and "checked and passed"
    are DISTINCT verdicts here. If an unproven op read as passing, this sweep
    lagging would silently manufacture positive training labels for every commit
    it had not reached yet — the worst failure this module could have, since the
    resulting corpus looks larger and better rather than broken.

    Bounded by `limit` on purpose, and it REPORTS what it left behind: an
    unbounded sweep on a 1,592-op log would run gates for hours on its first
    execution and be killed, which reads as "the sweep does not work". Oldest
    unproven first, so the backlog drains in order instead of thrashing the head.
    """
    from awgit.oplog import OpLog
    from awgit.prove import run_gates

    log = OutcomeLog(data_root=data_root)
    # "unknown" is NOT proven, and this line is the difference between a sweep
    # that catches up and one that permanently gives up.
    #
    # An outcome is recorded even when no gates ran — that is correct, since
    # "we did not check" has to be representable. But treating its presence as
    # proof would mean a single sweep executed while the gate plugin was
    # unregistered stamps every commit it touched as done, and no later sweep
    # would ever revisit them. The log would fill with `unknown`, the backlog
    # would read as drained, and the verifiable rewards this whole mechanism
    # exists to produce would never appear.
    #
    # Found by its own verification run: proving 3 commits with gates stubbed
    # out left them marked done with verdict `unknown`. Retryable now.
    proven = {o.git_sha for o in log.all_outcomes()
              if o.verdict != "unknown"}
    ops = OpLog(data_root=data_root).all_ops()

    # One entry per COMMIT, oldest first — gates run over a commit's file set,
    # not per changed function, so proving each op separately would run the same
    # gates dozens of times for one commit.
    by_sha: Dict[str, set] = {}
    for op in ops:
        if op.git_sha in proven:
            continue
        by_sha.setdefault(op.git_sha, set()).update(op.file_paths or [])

    pending = list(by_sha.items())
    result: Dict[str, Any] = {
        # AN EMPTY STORE IS NOT "NOTHING TO DO", and this distinction is the
        # whole reason the field exists.
        #
        # Measured 2026-08-16 on the fleet host: the AitherOS overlay resolves
        # vcs_data_root() to paths.Paths.DATA/vcs, which held **0 ops**, while
        # the 1,622 real ops the post-commit hook had written sat in the package
        # default at ~/.aither/awgit/data. A sweep pointed at the first one
        # returns "unproven_total: 0" and looks like a clean, finished backlog
        # forever — success reported on an empty directory.
        #
        # data_root.py's own overlay note predicts exactly this ("would repoint
        # every container at an empty store and the sweep routines would report
        # success on nothing"). It was written about containers; it came true on
        # the host. So the caller is told which store was read and how many ops
        # were in it, and `store_empty` is surfaced rather than folded into a
        # zero that reads as done.
        "data_root": str(data_root or vcs_data_root()),
        "ops_in_store": len(ops),
        "store_empty": not ops,
        "unproven_total": len(pending),
        "attempted": 0,
        "recorded": 0,
        "verdicts": {},
        # Named rather than implied: a bounded sweep that does not say what it
        # skipped is indistinguishable from one that finished the backlog.
        "left_unproven": max(0, len(pending) - limit),
    }
    if not ops:
        # Deliberately NOT an exception: a routine that raises here gets retried
        # and paged, and the fix is a configuration one. But it must never read
        # as a completed sweep, so the caller gets an explicit false.
        result["ok"] = False
        result["detail"] = (
            "the op store is EMPTY — this is a wrong-data_root symptom, not a "
            "drained backlog. Pass the store the post-commit hook writes to."
        )
        return result
    result["ok"] = True

    for sha, paths in pending[:limit]:
        result["attempted"] += 1
        gates = run_gates(sorted(paths))
        outcome = record_outcome(sha, gates, data_root=data_root)
        result["recorded"] += 1
        result["verdicts"][outcome.verdict] = (
            result["verdicts"].get(outcome.verdict, 0) + 1
        )
    return result
