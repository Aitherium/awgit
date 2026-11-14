"""The merge guard refuses a pull request nobody verified and nobody reviewed.

Measured 2026-11-12: 126 merges in a day; 20 of 20 sampled had cancelled checks
and 0 reviews. These tests pin the rule with a fake `gh`, so they need no
network and can fail.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))

from awgit import mergeguard as mg  # noqa: E402

REPO, PR, SHA = "Aitherium/AitherOS", 42, "a" * 40


class FakeGh:
    """Answers the four reads the guard makes, and records every write."""

    def __init__(self, statuses=(), check_runs=(), reviews=(), fail=()):
        self.statuses, self.check_runs, self.reviews = statuses, check_runs, reviews
        self.fail = set(fail)
        self.posts = []

    def __call__(self, argv):
        joined = " ".join(argv)
        if "-X POST" in joined:
            self.posts.append(argv)
            return 0, "{}", ""
        if joined.startswith("git rev-parse"):
            return (0, str(self.root) + "\n", "") if getattr(self, "root", None) else (1, "", "")
        if "api user" in joined:
            return 0, "owner\n", ""
        if f"pulls/{PR}/reviews" in joined:
            if "reviews" in self.fail:
                return 1, "", "HTTP 502"
            return 0, "\n".join(json.dumps(r) for r in self.reviews), ""
        if f"pulls/{PR}" in joined:
            if "pr" in self.fail:
                return 1, "", "HTTP 404"
            return 0, json.dumps({"sha": SHA, "state": "open"}), ""
        if "/status" in joined:
            if "statuses" in self.fail:
                return 1, "", "HTTP 502"
            return 0, "\n".join(json.dumps(s) for s in self.statuses), ""
        if "/check-runs" in joined:
            return 0, "\n".join(json.dumps(c) for c in self.check_runs), ""
        raise AssertionError(f"unexpected gh call: {joined}")


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path / "aither"))
    return tmp_path


def _decide(gh):
    return mg.evaluate(mg.fetch(REPO, PR, gh))


GREEN = {"context": "local-verify", "state": "success"}
REVIEW = {"state": "COMMENTED", "commit_id": SHA, "user": "aither"}


def test_a_pr_with_no_verification_and_no_review_is_refused():
    d = _decide(FakeGh())
    assert not d.allowed and d.exit_code == mg.EXIT_REFUSED
    assert any("local-verify" in r for r in d.reasons)
    assert any("no review" in r for r in d.reasons)


def test_local_verify_and_a_review_on_the_head_allow_the_merge():
    d = _decide(FakeGh(statuses=[GREEN], reviews=[REVIEW]))
    assert d.allowed and d.exit_code == 0
    assert d.verified_by == "local-verify" and "aither" in d.reviewed_by


def test_verification_alone_is_not_enough():
    d = _decide(FakeGh(statuses=[GREEN]))
    assert not d.allowed and any("no review" in r for r in d.reasons)


def test_a_review_alone_is_not_enough():
    d = _decide(FakeGh(reviews=[REVIEW]))
    assert not d.allowed and any("local-verify" in r for r in d.reasons)


@pytest.mark.parametrize("state", ["failure", "error", "pending"])
def test_a_local_verify_that_is_not_success_refuses(state):
    gh = FakeGh(statuses=[{"context": "local-verify", "state": state}], reviews=[REVIEW],
                check_runs=[{"name": "build", "status": "completed", "conclusion": "success"}])
    d = _decide(gh)
    assert not d.allowed, "green hosted checks must not outvote a red local-verify"
    assert state in " ".join(d.reasons)


def test_passing_hosted_checks_count_as_verification():
    gh = FakeGh(reviews=[REVIEW], check_runs=[
        {"name": "build", "status": "completed", "conclusion": "success"},
        {"name": "docs", "status": "completed", "conclusion": "skipped"}])
    d = _decide(gh)
    assert d.allowed and d.verified_by.startswith("hosted checks")


@pytest.mark.parametrize("run", [
    {"name": "build", "status": "completed", "conclusion": "cancelled"},
    {"name": "build", "status": "completed", "conclusion": "failure"},
    {"name": "build", "status": "queued", "conclusion": None},
])
def test_cancelled_failed_or_unfinished_hosted_checks_are_not_verification(run):
    ok = {"name": "lint", "status": "completed", "conclusion": "success"}
    d = _decide(FakeGh(reviews=[REVIEW], check_runs=[ok, run]))
    assert not d.allowed


def test_only_skipped_checks_verify_nothing():
    d = _decide(FakeGh(reviews=[REVIEW], check_runs=[
        {"name": "docs", "status": "completed", "conclusion": "skipped"}]))
    assert not d.allowed and "no hosted check succeeded" in " ".join(d.reasons)


def test_a_review_of_an_older_commit_does_not_count():
    stale = {"state": "APPROVED", "commit_id": "b" * 40, "user": "aither"}
    d = _decide(FakeGh(statuses=[GREEN], reviews=[stale]))
    assert not d.allowed and "older commit" in " ".join(d.reasons)


def test_changes_requested_on_the_head_refuses():
    blocking = {"state": "CHANGES_REQUESTED", "commit_id": SHA, "user": "hydra"}
    d = _decide(FakeGh(statuses=[GREEN], reviews=[REVIEW, blocking]))
    assert not d.allowed and "hydra" in " ".join(d.reasons)


def _write_record(verdict, sha=SHA):
    path = mg.review_record_path(REPO, PR, SHA)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"head_sha": sha, "verdict": verdict,
                                "findings_count": 2}), encoding="utf-8")


def test_a_local_review_record_for_the_head_counts():
    _write_record("reviewed")
    d = _decide(FakeGh(statuses=[GREEN]))
    assert d.allowed and d.reviewed_by.startswith("local review")


@pytest.mark.parametrize("verdict", ["could-not-judge", "", "changes-requested"])
def test_a_local_review_that_did_not_judge_or_found_blockers_does_not_count(verdict):
    _write_record(verdict)
    assert not _decide(FakeGh(statuses=[GREEN])).allowed


def _write_full(findings, *, verdict="reviewed", total=2, done=2, not_reviewed=()):
    path = mg.review_record_path(REPO, PR, SHA)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "head_sha": SHA, "verdict": verdict, "findings": findings,
        "findings_count": len(findings), "chunks_total": total, "chunks_reviewed": done,
        "not_reviewed": list(not_reviewed),
        "severity_counts": {s: sum(1 for f in findings if f["severity"] == s)
                            for s in ("blocker", "major", "minor", "nit")}}),
        encoding="utf-8")


def _finding(severity):
    return {"severity": severity, "file": "a.py", "line": 1, "problem": "p"}


def test_only_a_blocker_in_the_local_review_refuses():
    """MUTATION: refuse on any major finding. The rule is blocker-only: a major
    defect is noted and may be fixed in a later change."""
    _write_full([_finding("major"), _finding("minor"), _finding("nit")])
    d = _decide(FakeGh(statuses=[GREEN]))
    assert d.allowed and "no blocker" in d.reviewed_by and "1 major" in d.reviewed_by
    _write_full([_finding("blocker"), _finding("nit")], verdict="changes-requested")
    d = _decide(FakeGh(statuses=[GREEN]))
    assert not d.allowed and "1 blocker(s)" in " ".join(d.reasons)


def test_an_old_record_is_read_on_the_new_scale():
    _write_full([_finding("high")], verdict="changes-requested")
    assert _decide(FakeGh(statuses=[GREEN])).allowed, "high is major now: not a blocker"
    _write_full([_finding("critical")], verdict="changes-requested")
    assert not _decide(FakeGh(statuses=[GREEN])).allowed, "critical is a blocker"


@pytest.mark.parametrize("kwargs", [
    {"verdict": "partial", "total": 3, "done": 2,
     "not_reviewed": [{"chunk": "b.py", "reason": "model call failed: EOF"}]},
    {"verdict": "reviewed", "total": 3, "done": 2},          # an old record, no flag
    {"verdict": "reviewed", "total": 2, "done": 2,           # a cut-off answer
     "not_reviewed": [{"chunk": "a.py", "reason": "cut off"}]},
])
def test_a_partial_local_review_is_never_counted_as_a_review(kwargs):
    """MUTATION: count any record with a reviewed chunk. A review that lost a chunk
    to a dropped connection was then recorded as the whole review."""
    _write_full([_finding("nit")], **kwargs)
    d = _decide(FakeGh(statuses=[GREEN]))
    assert not d.allowed and "PARTIAL" in " ".join(d.reasons)
    assert "retries only those chunks" in " ".join(d.reasons)


def test_a_blocker_in_a_partial_review_is_reported_as_the_blocker():
    _write_full([_finding("blocker")], verdict="changes-requested", total=3, done=1)
    d = _decide(FakeGh(statuses=[GREEN]))
    assert not d.allowed and "blocker" in " ".join(d.reasons)


def test_the_comment_the_local_tool_posted_does_not_stand_in_for_its_record():
    """The local tool posts its (partial) review as a COMMENT. Counting that comment as
    a GitHub review would let the partial review through the back door."""
    _write_full([], verdict="partial", total=3, done=2,
                not_reviewed=[{"chunk": "c.py", "reason": "EOF"}])
    mirror = {"state": "COMMENTED", "commit_id": SHA, "user": "aither", "local": True}
    d = _decide(FakeGh(statuses=[GREEN], reviews=[mirror]))
    assert not d.allowed and "PARTIAL" in " ".join(d.reasons)
    person = {"state": "APPROVED", "commit_id": SHA, "user": "someone", "local": False}
    d = _decide(FakeGh(statuses=[GREEN], reviews=[mirror, person]))
    assert d.allowed and "someone" in d.reviewed_by


def test_a_local_blocker_refuses_even_beside_a_github_comment():
    _write_full([_finding("blocker")], verdict="changes-requested")
    for state in ("COMMENTED", "APPROVED"):
        other = {"state": state, "commit_id": SHA, "user": "someone"}
        d = _decide(FakeGh(statuses=[GREEN], reviews=[other]))
        assert not d.allowed and "blocker" in " ".join(d.reasons), state


def test_the_guard_asks_github_which_reviews_are_the_local_tools_own():
    seen = []

    class Recording(FakeGh):
        def __call__(self, argv):
            seen.append(" ".join(argv))
            return super().__call__(argv)

    _decide(Recording(statuses=[GREEN]))
    jq = [s for s in seen if "/reviews" in s][0]
    assert f'startswith("{mg.LOCAL_REVIEW_MARK}")' in jq


def test_an_unreadable_answer_is_a_refusal_never_a_pass():
    for broken in ("pr", "statuses", "reviews"):
        d = _decide(FakeGh(statuses=[GREEN], reviews=[REVIEW], fail=[broken]))
        assert not d.allowed and d.exit_code == mg.EXIT_DEAD, broken


def test_guard_prints_the_repositorys_own_way_to_get_verified(tmp_path):
    hints = tmp_path / ".github" / "merge-guard-hints.txt"
    hints.parent.mkdir()
    hints.write_text("# how\nverify: tool {pr} --post\n\nreview: other {pr}\n",
                     encoding="utf-8")
    gh, out = FakeGh(), io.StringIO()
    gh.root = tmp_path
    code = mg.guard(REPO, PR, run=gh, out=out)
    text = out.getvalue()
    assert code == 1 and "REFUSING" in text
    assert "verify: tool 42 --post" in text and "review: other 42" in text
    assert "# how" not in text


def test_guard_still_says_what_is_needed_when_the_repository_gives_no_hints():
    out = io.StringIO()
    assert mg.guard(REPO, PR, run=FakeGh(), out=out) == 1
    assert "local-verify" in out.getvalue() and "recorded review" in out.getvalue()


def test_override_is_refused_without_a_terminal_and_writes_nothing(_home):
    gh, out = FakeGh(), io.StringIO()
    code = mg.guard(REPO, PR, "the hosted lane is down and this is a hotfix", run=gh,
                    is_tty=False, out=out)
    assert code == 1 and "interactive terminal" in out.getvalue()
    assert not gh.posts
    assert not (_home / "aither" / "merge-guard" / "overrides.jsonl").exists()


def test_override_needs_a_real_reason_and_the_typed_pr_number():
    gh = FakeGh()
    assert mg.guard(REPO, PR, "why not", run=gh, is_tty=True, confirm=lambda _: "42",
                    out=io.StringIO()) == 1
    assert mg.guard(REPO, PR, "the hosted lane is down, hotfix", run=gh, is_tty=True,
                    confirm=lambda _: "41", out=io.StringIO()) == 1
    assert not gh.posts


def test_a_confirmed_override_is_logged_and_posted_on_the_commit(_home):
    gh, out = FakeGh(), io.StringIO()
    code = mg.guard(REPO, PR, "the hosted lane is down, hotfix for the outage", run=gh,
                    is_tty=True, confirm=lambda _: "42", out=out)
    assert code == 0
    log = _home / "aither" / "merge-guard" / "overrides.jsonl"
    entry = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["pr"] == PR and entry["head_sha"] == SHA and entry["by"] == "owner"
    assert entry["refused_for"], "the override must record what it overrode"
    assert len(gh.posts) == 1
    post = " ".join(gh.posts[0])
    assert f"statuses/{SHA}" in post and "context=merge-guard" in post
    assert "state=success" not in post, "an override must never read as a pass"


def test_owner_override_flag_is_split_from_gh_arguments():
    assert mg.split_override(["42", "--squash", "--owner-override", "because x"]) == (
        ["42", "--squash"], "because x")
    assert mg.split_override(["--owner-override=r", "42"]) == (["42"], "r")
    assert mg.split_override(["42", "--squash"]) == (["42", "--squash"], None)


def test_pr_number_is_read_past_flag_values():
    assert mg.pr_number_from(["--repo", "o/r", "42", "--squash"]) == 42
    assert mg.pr_number_from(["https://github.com/o/r/pull/77"]) == 77
    assert mg.repo_from(["-R", "o/r", "42"]) == "o/r"


def test_awgit_pr_merge_refuses_before_gh_is_asked_to_merge(monkeypatch):
    """`awgit pr merge 42` must stop at the guard: gh is never run for the merge."""
    import awgit.cli as cli

    monkeypatch.setattr(mg, "_run", FakeGh())
    monkeypatch.setattr(mg, "guard",
                        lambda repo, number, override=None, **kw:
                        mg.evaluate(mg.fetch(repo, number, FakeGh())).exit_code)
    monkeypatch.setattr("awgit.changeid.of_commit", lambda ref: "")
    merges = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda argv, *a, **k: merges.append(argv) or type(
                            "P", (), {"returncode": 0})())
    code = cli.main(["pr", "merge", "42", "-R", REPO, "--squash"])
    assert code == 1
    assert not any(a[:3] == ["gh", "pr", "merge"] for a in merges)


def test_a_local_review_record_that_is_not_an_object_is_a_refusal_not_a_crash():
    path = mg.review_record_path(REPO, PR, SHA)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2]", encoding="utf-8")
    d = _decide(FakeGh(statuses=[GREEN]))
    assert not d.allowed and d.exit_code == mg.EXIT_DEAD
    assert "not a JSON object" in " ".join(d.reasons)


def test_awgit_pr_merge_with_no_method_never_reaches_ghs_prompt(monkeypatch):
    """`gh pr merge 42` with no method prompts for one; an agent's shell cannot answer,
    and #16575, #16622, #16630 hung for hours. awgit names squash when none is given."""
    import awgit.cli as cli

    monkeypatch.setattr(mg, "guard", lambda repo, number, override=None, **kw: 0)
    monkeypatch.setattr("awgit.changeid.of_commit", lambda ref: "")
    runs = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda argv, *a, **k: runs.append(argv) or type(
                            "P", (), {"returncode": 0})())
    assert cli.main(["pr", "merge", "42", "-R", REPO]) == 0
    merge = [a for a in runs if a[:3] == ["gh", "pr", "merge"]][-1]
    assert "--squash" in merge
    runs.clear()
    assert cli.main(["pr", "merge", "42", "-R", REPO, "--rebase"]) == 0
    merge = [a for a in runs if a[:3] == ["gh", "pr", "merge"]][-1]
    assert "--rebase" in merge and "--squash" not in merge
