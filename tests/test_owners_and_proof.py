"""Ownership measured from history, and evidence that cannot fake a pass.

Two rules carry these features and both fail quietly if they are wrong:

**CODEOWNERS precedence.** git takes the LAST matching rule. Get it backwards
and the generic `*` at the top of most files beats every specific rule below —
not a crash, just a confidently wrong reviewer on every path.

**"Nothing ran" must not read as "passed".** A gate that exits 2 could not
judge, and an empty gate list means nothing checked the change at all. Both are
the absence of a verdict. Spelling either of them the same way as success is how
"the checker is broken" gets filed as "the code is fine".
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit import owners, plugins, prove  # noqa: E402

CODEOWNERS = """
# the generic rule comes FIRST, as it does in most real files
*                 @everyone
/docs/            @writers
platform/lib/**   @platform
*.py              @pythonistas
"""


def test_the_last_matching_codeowners_rule_wins():
    """git's precedence. Backwards, `*` beats every specific rule below it."""
    assert owners._match(CODEOWNERS, "notes.txt") == ["@everyone"]
    assert owners._match(CODEOWNERS, "docs/guide.md") == ["@writers"]
    # `*.py` is listed AFTER the lib rule, so it wins for a .py inside lib.
    assert owners._match(CODEOWNERS, "platform/lib/x.py") == ["@pythonistas"]
    assert owners._match(CODEOWNERS, "platform/lib/x.go") == ["@platform"]


def test_an_unmatched_path_has_no_declared_owner_rather_than_a_wrong_one():
    assert owners._match("/docs/  @writers\n", "src/main.py") == []


def test_ownership_decays_with_age():
    """Someone who has not touched a module in a year does not own it today."""
    fresh = owners._weight("2999-01-01T00:00:00")     # far future => ~1.0
    old = owners._weight("1990-01-01T00:00:00")       # long past  => ~0.0
    assert fresh > old
    assert 0.0 <= old < 0.01, old


def test_an_unparseable_timestamp_is_not_scored_as_brand_new():
    """Garbage must not outrank real recent work; one half-life is the honest guess."""
    assert owners._weight("not-a-date") == 0.5


def test_a_bundle_with_no_gates_is_not_proved():
    """THE rule. An empty gate list is the commonest way a change arrives with a
    green tick and nothing behind it."""
    bundle = prove.Bundle(gates=[])
    assert not bundle.proved
    assert bundle.to_dict()["proved"] is False


def test_a_dead_gate_is_not_a_pass_and_not_a_failure():
    bundle = prove.Bundle(gates=[
        prove.GateResult("lint", prove.OK),
        prove.GateResult("fleet", prove.DEAD, "could not reach the host"),
    ])
    assert not bundle.proved, "a gate that could not judge cannot prove anything"
    assert bundle.violations == [], "DEAD is not a violation of the code"
    assert len(bundle.dead) == 1


def test_all_ok_with_at_least_one_gate_is_proved():
    bundle = prove.Bundle(gates=[prove.GateResult("lint", prove.OK)])
    assert bundle.proved


def test_an_unknown_gate_status_degrades_to_dead_not_ok():
    """A host inventing its own vocabulary must not accidentally report success."""
    plugins.register(plugins.GATES, lambda paths: [
        {"name": "weird", "status": "probably-fine"}])
    try:
        results = prove.run_gates([])
    finally:
        plugins.unregister(plugins.GATES)
    assert [r.status for r in results] == [prove.DEAD]


def test_a_registered_gate_runner_reaches_the_bundle():
    plugins.register(plugins.GATES, lambda paths: [
        {"name": "ruff", "status": "ok"},
        {"name": "types", "status": "violation", "detail": "3 errors"}])
    try:
        results = prove.run_gates(["a.py"])
    finally:
        plugins.unregister(plugins.GATES)
    bundle = prove.Bundle(gates=results)
    assert not bundle.proved
    assert [g.name for g in bundle.violations] == ["types"]


def test_the_markdown_says_which_gates_could_not_run():
    bundle = prove.Bundle(gates=[prove.GateResult("fleet", prove.DEAD)])
    md = prove.to_markdown(bundle)
    assert "NOT proved" in md
    assert "could not run" in md


def test_markdown_with_no_gates_says_nothing_verified_it():
    md = prove.to_markdown(prove.Bundle())
    assert "nothing verified this change" in md
