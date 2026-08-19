"""Test outcome tracking and the unknown vs passed distinction.

The core property: "unknown" (no outcomes recorded) must be distinct from "passed"
(outcomes recorded, all ok). Conflating these would poison training signals — a
commit nobody checked would appear identical to one that passed all checks.

These tests use mutation guards to prove the distinction cannot collapse.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from awgit.outcomes import OutcomeLog, record_outcome
from awgit.schema import Outcome, OutcomeGate


class TestOutcomeVerdictClassification:
    """Verify outcome.verdict() classifies correctly and cannot be regressed."""

    def test_unknown_when_no_gates(self) -> None:
        """No gates recorded = unknown, not passed."""
        outcome = Outcome(outcome_id="test-1", git_sha="abc123", ts="2026-01-01T00:00:00")
        assert outcome.verdict == "unknown"
        assert not outcome.gates

    def test_passed_when_all_ok(self) -> None:
        """All gates ok = passed."""
        gates = [
            OutcomeGate(name="lint", status="ok"),
            OutcomeGate(name="test", status="ok"),
        ]
        outcome = Outcome(outcome_id="test-2", git_sha="abc123", ts="2026-01-01T00:00:00", gates=gates)
        assert outcome.verdict == "passed"
        assert len(outcome.gates) == 2

    def test_failed_when_violation(self) -> None:
        """One violation = failed."""
        gates = [
            OutcomeGate(name="lint", status="ok"),
            OutcomeGate(name="security", status="violation", detail="uses verify=False"),
        ]
        outcome = Outcome(outcome_id="test-3", git_sha="abc123", ts="2026-01-01T00:00:00", gates=gates)
        assert outcome.verdict == "failed"

    def test_failed_when_dead(self) -> None:
        """One dead gate = failed (cannot judge)."""
        gates = [
            OutcomeGate(name="lint", status="ok"),
            OutcomeGate(name="fleet_check", status="dead", detail="fleet unreachable"),
        ]
        outcome = Outcome(outcome_id="test-4", git_sha="abc123", ts="2026-01-01T00:00:00", gates=gates)
        assert outcome.verdict == "failed"

    def test_failed_when_multiple_violations(self) -> None:
        """Multiple violations = failed."""
        gates = [
            OutcomeGate(name="lint", status="violation", detail="line too long"),
            OutcomeGate(name="type", status="violation", detail="missing type hint"),
        ]
        outcome = Outcome(outcome_id="test-5", git_sha="abc123", ts="2026-01-01T00:00:00", gates=gates)
        assert outcome.verdict == "failed"


class TestVerdictMutationGuards:
    """Mutation guards to prove the unknown vs passed distinction cannot collapse.

    These tests create "mutant" verdicts and prove they fail — so if a future
    change accidentally regresses the distinction, the test fails loudly instead
    of a training signal becoming poisoned.
    """

    def test_mutation_guard_unknown_not_ok(self) -> None:
        """MUTATION: returning 'ok' for unknown (no gates) would fail this."""
        outcome = Outcome(outcome_id="mg-1", git_sha="abc", ts="2026-01-01T00:00:00")
        # If someone changed verdict to return "ok" when gates is empty:
        # assert outcome.verdict == "ok"  # MUTANT — would pass wrongly
        assert outcome.verdict == "unknown"  # CORRECT

    def test_mutation_guard_empty_gates_not_passed(self) -> None:
        """MUTATION: treating empty gate list as passed would fail this."""
        outcome1 = Outcome(outcome_id="mg-2a", git_sha="abc", ts="2026-01-01T00:00:00", gates=[])
        outcome2 = Outcome(
            outcome_id="mg-2b",
            git_sha="abc",
            ts="2026-01-01T00:00:00",
            gates=[OutcomeGate(name="test", status="ok")],
        )
        # The verdicts MUST differ for the distinction to hold:
        assert outcome1.verdict == "unknown"
        assert outcome2.verdict == "passed"
        assert outcome1.verdict != outcome2.verdict

    def test_mutation_guard_ok_gate_but_dead_gate(self) -> None:
        """MUTATION: ignoring dead gates in verdict classification would fail this."""
        gates = [
            OutcomeGate(name="lint", status="ok"),
            OutcomeGate(name="fleet_check", status="dead"),
        ]
        outcome = Outcome(outcome_id="mg-3", git_sha="abc", ts="2026-01-01T00:00:00", gates=gates)
        # If someone changed the verdict logic to ignore dead gates:
        # assert outcome.verdict == "passed"  # MUTANT — would pass wrongly
        assert outcome.verdict == "failed"  # CORRECT

    def test_mutation_guard_single_violation_still_failed(self) -> None:
        """MUTATION: treating one violation as passed would fail this."""
        gates = [
            OutcomeGate(name="lint", status="ok"),
            OutcomeGate(name="lint", status="ok"),
            OutcomeGate(name="lint", status="ok"),
            OutcomeGate(name="security", status="violation"),
        ]
        outcome = Outcome(outcome_id="mg-4", git_sha="abc", ts="2026-01-01T00:00:00", gates=gates)
        # If someone changed verdict to only fail on multiple violations:
        # assert outcome.verdict == "passed"  # MUTANT — would pass wrongly
        assert outcome.verdict == "failed"  # CORRECT


class TestOutcomePersistence:
    """Verify outcomes are durably recorded and recoverable."""

    def test_record_outcome_creates_file(self, tmp_path: Path) -> None:
        """record_outcome() creates outcomes.jsonl if needed."""
        gate1 = type("GateResult", (), {"name": "test", "status": "ok", "detail": ""})()
        gate2 = type("GateResult", (), {"name": "lint", "status": "ok", "detail": ""})()

        outcome = record_outcome("abc123def", [gate1, gate2], data_root=tmp_path)

        assert outcome.outcome_id.startswith("abc123def")
        assert outcome.git_sha == "abc123def"
        assert outcome.verdict == "passed"
        assert len(outcome.gates) == 2

        # Verify the file was created
        outcomes_file = tmp_path / "outcomes.jsonl"
        assert outcomes_file.exists()

    def test_outcome_roundtrip_through_json(self) -> None:
        """Outcome to_dict() and from_dict() preserves the record."""
        gates = [
            OutcomeGate(name="lint", status="ok"),
            OutcomeGate(name="test", status="violation", detail="1 test failed"),
        ]
        original = Outcome(
            outcome_id="test-rt",
            git_sha="abc123",
            ts="2026-01-01T12:34:56",
            gates=gates,
        )
        as_dict = original.to_dict()
        assert isinstance(as_dict, dict)
        assert as_dict["outcome_id"] == "test-rt"
        assert as_dict["verdict"] == "failed"
        assert len(as_dict["gates"]) == 2

        recovered = Outcome.from_dict(as_dict)
        assert recovered.outcome_id == original.outcome_id
        assert recovered.git_sha == original.git_sha
        assert recovered.verdict == original.verdict
        assert len(recovered.gates) == len(original.gates)

    def test_outcome_log_append_idempotent(self, tmp_path: Path) -> None:
        """Appending the same outcome twice should be idempotent."""
        outcome = Outcome(
            outcome_id="idempotent-test",
            git_sha="abc123",
            ts="2026-01-01T00:00:00",
            gates=[OutcomeGate(name="test", status="ok")],
        )

        log = OutcomeLog(data_root=tmp_path)
        log.append(outcome)
        log.append(outcome)  # should not duplicate

        log2 = OutcomeLog(data_root=tmp_path)
        assert len(log2.all_outcomes()) == 1
        assert log2.all_outcomes()[0].outcome_id == "idempotent-test"

    def test_outcomes_for_commit(self, tmp_path: Path) -> None:
        """Query outcomes by git_sha."""
        log = OutcomeLog(data_root=tmp_path)
        o1 = Outcome(outcome_id="o1", git_sha="sha1", ts="2026-01-01T00:00:00")
        o2 = Outcome(outcome_id="o2", git_sha="sha1", ts="2026-01-01T00:01:00")
        o3 = Outcome(outcome_id="o3", git_sha="sha2", ts="2026-01-01T00:02:00")

        log.append(o1)
        log.append(o2)
        log.append(o3)

        sha1_outcomes = log.outcomes_for_commit("sha1")
        assert len(sha1_outcomes) == 2
        assert sha1_outcomes[0].outcome_id == "o1"
        assert sha1_outcomes[1].outcome_id == "o2"

        sha2_outcomes = log.outcomes_for_commit("sha2")
        assert len(sha2_outcomes) == 1
        assert sha2_outcomes[0].outcome_id == "o3"

    def test_latest_outcome(self, tmp_path: Path) -> None:
        """latest_outcome() returns the most recent, not the first."""
        log = OutcomeLog(data_root=tmp_path)
        o1 = Outcome(
            outcome_id="o1",
            git_sha="sha1",
            ts="2026-01-01T00:00:00",
            gates=[OutcomeGate(name="test", status="ok")],
        )
        time.sleep(0.01)
        o2 = Outcome(
            outcome_id="o2",
            git_sha="sha1",
            ts="2026-01-01T00:01:00",
            gates=[OutcomeGate(name="test", status="violation")],
        )

        log.append(o1)
        log.append(o2)

        latest = log.latest_outcome("sha1")
        assert latest is not None
        assert latest.outcome_id == "o2"
        assert latest.verdict == "failed"

    def test_latest_outcome_none_when_not_checked(self, tmp_path: Path) -> None:
        """latest_outcome() returns None if a commit was never checked."""
        log = OutcomeLog(data_root=tmp_path)
        latest = log.latest_outcome("never-checked-sha")
        assert latest is None


class TestOutcomeGateDetail:
    """Verify gate details are captured and truncated correctly."""

    def test_gate_detail_truncated_at_400_chars(self) -> None:
        """OutcomeGate truncates detail to 400 chars in to_dict()."""
        long_detail = "x" * 500
        gate = OutcomeGate(name="test", status="violation", detail=long_detail)
        as_dict = gate.to_dict()
        assert len(as_dict["detail"]) == 400
        assert as_dict["detail"] == "x" * 400

    def test_gate_roundtrip_preserves_truncated_detail(self) -> None:
        """Truncated detail survives roundtrip through from_dict()."""
        long_detail = "error: " + ("x" * 500)
        gate = OutcomeGate(name="test", status="violation", detail=long_detail)
        as_dict = gate.to_dict()
        recovered = OutcomeGate.from_dict(as_dict)
        assert len(recovered.detail) == 400


class TestOutcomeSchemaVersioning:
    """Verify forward/backward compatibility of the Outcome schema."""

    def test_future_schema_rejected(self) -> None:
        """A record with a newer schema_version is refused (not silently misparsed)."""
        future_record = {
            "outcome_id": "future",
            "git_sha": "abc123",
            "ts": "2026-01-01T00:00:00",
            "gates": [],
            "verdict": "unknown",
            "schema_version": 999,
        }
        with pytest.raises(ValueError, match="schema_version.*newer"):
            Outcome.from_dict(future_record)

    def test_missing_required_fields(self) -> None:
        """Missing outcome_id or git_sha is an error."""
        with pytest.raises(ValueError, match="outcome_id"):
            Outcome.from_dict({"git_sha": "abc123", "ts": "2026-01-01T00:00:00"})

        with pytest.raises(ValueError, match="git_sha"):
            Outcome.from_dict({"outcome_id": "o1", "ts": "2026-01-01T00:00:00"})

    def test_old_record_without_gates_field(self) -> None:
        """A record with no gates field parses (gates defaults to [])."""
        old_record = {
            "outcome_id": "old",
            "git_sha": "abc123",
            "ts": "2026-01-01T00:00:00",
        }
        outcome = Outcome.from_dict(old_record)
        assert outcome.gates == []
        assert outcome.verdict == "unknown"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_an_empty_op_store_is_a_refusal_not_a_drained_backlog(tmp_path):
    """`unproven_total: 0` from an empty store must not read as "all done".

    MEASURED 2026-08-16 on the fleet host. The AitherOS overlay resolves
    vcs_data_root() to paths.Paths.DATA/vcs, which held ZERO ops, while the
    1,622 ops the post-commit hook had actually written sat in the package
    default at ~/.aither/awgit/data. A sweep pointed at the first store returns
    a clean, finished-looking result forever — success reported on an empty
    directory, which is this repo's single most-repeated defect class.

    data_root.py's own overlay note predicts exactly this ("would repoint every
    container at an empty store and the sweep routines would report success on
    nothing"). It was written about containers and came true on the host.
    """
    from awgit.outcomes import sweep_unproven

    r = sweep_unproven(limit=5, data_root=tmp_path)
    assert r["store_empty"] is True
    assert r["ok"] is False, (
        "an empty op store reported a successful sweep — indistinguishable "
        "from a drained backlog, which is how this silently never runs"
    )
    assert "data_root" in r and str(tmp_path) in r["data_root"], (
        "the sweep does not say WHICH store it read, so a wrong-root result "
        "cannot be told apart from a right-root one"
    )


def test_an_unknown_verdict_is_retried_not_treated_as_proven(tmp_path, monkeypatch):
    """`unknown` must stay in the queue, or one bad sweep gives up permanently.

    An outcome is recorded even when no gates ran — "we did not check" has to
    be representable. But if the SWEEP treats its presence as proof, a single
    run executed while the gate plugin was unregistered stamps every commit it
    touched as done and no later sweep revisits them: the log fills with
    `unknown`, the backlog reads as drained, and the verifiable rewards this
    mechanism exists to produce never appear.

    Found by this module's own verification run, which proved three commits
    with gates stubbed out and left them marked done.
    """
    from awgit import outcomes as outcomes_mod

    outcomes_mod.record_outcome("sha-unproven", [], data_root=tmp_path)

    class _Op:
        git_sha = "sha-unproven"
        file_paths = ["a.py"]

    monkeypatch.setattr(outcomes_mod, "OutcomeLog", outcomes_mod.OutcomeLog)
    import awgit.oplog as _oplog
    monkeypatch.setattr(_oplog.OpLog, "all_ops", lambda self: [_Op()])
    import awgit.prove as _prove
    monkeypatch.setattr(_prove, "run_gates", lambda paths: [])

    r = outcomes_mod.sweep_unproven(limit=5, data_root=tmp_path)
    assert r["unproven_total"] == 1, (
        "a commit whose only outcome is `unknown` was treated as proven; it "
        "will never be re-verified and its reward is lost for good"
    )

    # ...and the mirror image: a REAL verdict must NOT be retried forever, or
    # the sweep re-runs the host's 30-180s gates on the whole history each pass
    # and is switched off for being unusable.
    # The REAL GateResult, not a duck-typed stand-in. The first version of
    # this test used a bare class with the right attribute names and produced
    # verdict `unknown` anyway — so it asserted the retry path twice and never
    # exercised the stop path at all. A stub that does not reach the code under
    # test turns a two-sided guard into a one-sided one, silently.
    from awgit.prove import GateResult

    outcomes_mod.record_outcome("sha-unproven", [GateResult(name="g", status="ok")],
                     data_root=tmp_path)
    r2 = outcomes_mod.sweep_unproven(limit=5, data_root=tmp_path)
    assert r2["unproven_total"] == 0, (
        "a commit with a real verdict is still queued — the sweep would redo "
        "every gate on every run"
    )


def test_two_outcomes_for_one_commit_in_the_same_second_both_survive(tmp_path):
    """The outcome_id must be content-addressed, not clock-addressed.

    It was `f"{git_sha[:16]}-{int(time.time())}"` — one-second resolution — and
    `OutcomeLog.append` is idempotent by outcome_id. So two outcomes for one
    commit inside the same second collapsed to one, and the loser was dropped
    SILENTLY: append returns normally and record_outcome hands back an Outcome
    that was never persisted, so no caller can tell it did not land.

    It bit the moment the retry path existed. A sweep re-proving a commit that
    had been recorded `unknown` moments earlier discarded the REAL verdict and
    kept the unknown — turning the retry into a no-op, invisibly. Found by this
    file's own sibling test failing for what looked like the wrong reason.

    Both directions are asserted, because a fix in one direction alone is worse
    than the bug: dropping idempotence entirely would append a duplicate row on
    every sweep pass and grow the log without bound.
    """
    from awgit.outcomes import OutcomeLog, record_outcome
    from awgit.prove import GateResult

    # DIFFERENT results for one commit, same second -> two rows.
    record_outcome("s", [], data_root=tmp_path)
    record_outcome("s", [GateResult(name="g", status="ok")], data_root=tmp_path)
    verdicts = [o.verdict for o in OutcomeLog(data_root=tmp_path).all_outcomes()]
    assert verdicts == ["unknown", "passed"], (
        f"a real verdict was dropped by an id collision: {verdicts}. A commit "
        f"recorded `unknown` can then never be re-proven, and nothing errors"
    )

    # IDENTICAL result -> still idempotent, or every sweep pass grows the log.
    record_outcome("s", [GateResult(name="g", status="ok")], data_root=tmp_path)
    again = [o.verdict for o in OutcomeLog(data_root=tmp_path).all_outcomes()]
    assert again == verdicts, (
        f"re-recording an identical outcome appended a duplicate: {again}"
    )
