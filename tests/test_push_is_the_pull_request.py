"""One PR per commit, based on the commit below it, found again after an amend.

Three properties carry the model, and each fails silently if it is wrong:

**Base chaining.** If every PR were based on trunk, each would show the whole
stack's diff and the review would be exactly the 900-line branch this replaces.
Nothing errors — the PRs open, they are just useless.

**Ref naming.** The Change-Id is IN the ref, which is what makes "does this
change already have a PR" a lookup rather than stored state that goes stale.

**Amend stability.** Push after `commit --amend` must UPDATE, not open a second
PR. This is the one an author hits on their first review comment, and getting it
wrong produces a duplicate PR rather than an error.

GitHub is never contacted: the plan is the decidable part, and it is what the
apply step consumes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit import changeid, push  # noqa: E402
from awgit.bridge import install_hooks  # noqa: E402

ACTOR = "claude:8f21ab"


def git(repo: Path, *args: str, check: bool = True) -> str:
    env = dict(os.environ)
    env["VCS_LEASES_ENFORCE"] = "0"
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=env)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "T")
    install_hooks(str(r))
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "vcs"))
    monkeypatch.setenv("VCS_LEASES_ENFORCE", "0")
    monkeypatch.chdir(r)
    (r / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(r, "add", "seed.txt")
    git(r, "commit", "-q", "-m", "seed")
    git(r, "branch", "-f", "trunk")
    return r


def commit(repo: Path, name: str, body: str, message: str) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def test_each_pr_is_based_on_the_one_below_it(repo: Path):
    """THE property. All-based-on-trunk opens fine and reviews terribly."""
    commit(repo, "a.py", "def a():\n    return 1\n", "first")
    commit(repo, "b.py", "def b():\n    return 2\n", "second")
    commit(repo, "c.py", "def c():\n    return 3\n", "third")

    plan = push.plan(ACTOR, repo, trunk="trunk", query_github=False)
    assert not plan.problems, plan.problems
    assert [s.subject for s in plan.steps] == ["first", "second", "third"]

    assert plan.steps[0].base == "trunk", "the bottom must sit on trunk"
    assert plan.steps[1].base == plan.steps[0].ref
    assert plan.steps[2].base == plan.steps[1].ref, (
        "each PR must be based on the ref below it, or every PR shows the whole "
        "stack's diff"
    )


def test_the_ref_carries_the_change_id(repo: Path):
    commit(repo, "a.py", "def a():\n    return 1\n", "first")
    plan = push.plan(ACTOR, repo, trunk="trunk", query_github=False)
    step = plan.steps[0]
    assert step.change_id.startswith("I")
    assert step.change_id[1:13] in step.ref, (
        "the Change-Id must be IN the ref — that is what makes the PR lookup "
        "stateless"
    )
    assert push.is_synthetic(step.ref)
    assert step.ref.startswith("awgit/")


def test_an_amend_keeps_the_same_ref_so_the_pr_is_updated(repo: Path):
    """The first review comment. Getting this wrong opens a duplicate PR."""
    commit(repo, "a.py", "def a():\n    return 1\n", "first")
    before = push.plan(ACTOR, repo, trunk="trunk", query_github=False).steps[0]

    (repo / "a.py").write_text("def a():\n    return 2\n", encoding="utf-8")
    git(repo, "add", "a.py")
    git(repo, "commit", "-q", "--amend", "--no-edit")

    after = push.plan(ACTOR, repo, trunk="trunk", query_github=False).steps[0]
    assert after.sha != before.sha, "the amend did not rewrite the commit"
    assert after.ref == before.ref, (
        "the amended commit must publish to the SAME ref, or push opens a second "
        "pull request for one change"
    )

    # With that ref already carrying a PR, the plan must UPDATE it.
    plan = push.plan(ACTOR, repo, trunk="trunk", query_github=False)
    plan.steps[0].existing_pr = 42
    assert plan.steps[0].action == "update"


def test_a_commit_without_a_change_id_is_refused_with_a_fix(repo: Path):
    """Pushing it would open a PR that no later amend could ever find again."""
    from awgit.bridge import uninstall_hooks

    uninstall_hooks(str(repo))
    commit(repo, "a.py", "def a():\n    return 1\n", "unstamped")
    assert changeid.of_commit("HEAD", repo) is None

    plan = push.plan(ACTOR, repo, trunk="trunk", query_github=False)
    assert plan.problems, "an unstamped commit must not be pushed"
    assert "hooks install" in plan.problems[0], "the refusal must name the fix"
    assert not plan.steps


def test_force_push_is_confined_to_the_awgit_namespace():
    """The one guard whose failure destroys somebody's branch."""
    assert push.is_synthetic("awgit/claude-8f21/abc123def456")
    for hostile in ("main", "develop", "refs/heads/main", "awgit", "awgit/x",
                    "feature/awgit/main", ""):
        assert not push.is_synthetic(hostile), hostile


def test_an_actor_with_shell_characters_makes_a_valid_ref():
    """The actor is caller-supplied and lands in a REFNAME.

    git rejects any refname containing "..", so a slug that preserved dots
    turned `claude:8f21/../../etc` into a push failing with "invalid refname" —
    naming a ref the user never typed.
    """
    ref = push.synthetic_ref("claude:8f21/../../etc", "I" + "a" * 40)
    assert push.is_synthetic(ref), ref
    for illegal in ("..", ":", "~", "^", "?", "*", "[", "\\", " "):
        assert illegal not in ref, f"{illegal!r} is not legal in a git refname: {ref}"
    assert not push.is_synthetic("awgit/x/.."), "the guard must reject it too"


def test_the_body_shows_the_whole_stack(repo: Path):
    commit(repo, "a.py", "def a():\n    return 1\n", "first")
    commit(repo, "b.py", "def b():\n    return 2\n", "second")
    plan = push.plan(ACTOR, repo, trunk="trunk", query_github=False)
    body = push.body_for(plan.steps[0], plan)
    assert "first" in body and "second" in body
    assert "1 of 2" in body
    assert plan.steps[0].change_id in body


# ── apply: the half that actually writes ──────────────────────────────────
#
# Everything above tests the PLAN. The plan is the decidable part, but apply is
# the part that force-pushes refs and opens pull requests, and until this block
# existed it had never been executed once. The push here is REAL, against a
# local bare remote, so the ref names and the namespace guard are proven rather
# than described; only `gh` is recorded instead of called, because a test must
# not open pull requests.


@pytest.fixture
def repo_with_remote(repo: Path, tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git(repo, "remote", "add", "origin", str(bare))
    return repo


class RecordingGh:
    """Stands in for `gh`, recording argv and reporting success."""

    def __init__(self):
        self.calls: list = []

    def __call__(self, repo, *args):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(
            args=list(args), returncode=0,
            stdout="https://github.test/o/r/pull/7\n", stderr="")

    def verbs(self):
        return [" ".join(a[:2]) for a in self.calls]


def test_apply_pushes_real_refs_and_opens_one_pr_per_commit(
        repo_with_remote: Path, monkeypatch):
    repo = repo_with_remote
    commit(repo, "a.py", "def a():\n    return 1\n", "first")
    commit(repo, "b.py", "def b():\n    return 2\n", "second")

    fake = RecordingGh()
    monkeypatch.setattr(push, "_gh", fake)
    monkeypatch.setattr(push, "gh_available", lambda repo=None: (True, ""))

    plan = push.plan(ACTOR, repo, trunk="trunk", query_github=False)
    ok, messages = push.apply(plan, repo)
    assert ok, messages

    # The refs really exist on the remote, under the awgit namespace only.
    remote_refs = git(repo, "ls-remote", "--heads", "origin").split()
    names = [r.replace("refs/heads/", "") for r in remote_refs if r.startswith("refs/")]
    assert sorted(names) == sorted(s.ref for s in plan.steps), names
    assert all(n.startswith("awgit/") for n in names), (
        f"push wrote outside its namespace: {names}")

    # One `pr create` per commit, and the bases chain.
    creates = [a for a in fake.calls if a[:2] == ["pr", "create"]]
    assert len(creates) == 2, fake.verbs()
    bases = [a[a.index("--base") + 1] for a in creates]
    assert bases[0] == "trunk"
    assert bases[1] == plan.steps[0].ref, "the second PR must sit on the first"


def test_apply_updates_an_existing_pr_instead_of_opening_another(
        repo_with_remote: Path, monkeypatch):
    """The amend path, end to end. A second `pr create` here is a duplicate PR."""
    repo = repo_with_remote
    commit(repo, "a.py", "def a():\n    return 1\n", "first")

    fake = RecordingGh()
    monkeypatch.setattr(push, "_gh", fake)
    monkeypatch.setattr(push, "gh_available", lambda repo=None: (True, ""))

    plan = push.plan(ACTOR, repo, trunk="trunk", query_github=False)
    plan.steps[0].existing_pr = 42
    ok, messages = push.apply(plan, repo)
    assert ok, messages

    assert [a[:2] for a in fake.calls] == [["pr", "edit"]], fake.verbs()
    assert "42" in fake.calls[0]


def test_apply_refuses_a_ref_outside_the_namespace(repo_with_remote: Path, monkeypatch):
    """The guard that stands between a force-push and somebody's branch."""
    repo = repo_with_remote
    commit(repo, "a.py", "def a():\n    return 1\n", "first")
    fake = RecordingGh()
    monkeypatch.setattr(push, "_gh", fake)
    monkeypatch.setattr(push, "gh_available", lambda repo=None: (True, ""))

    plan = push.plan(ACTOR, repo, trunk="trunk", query_github=False)
    plan.steps[0].ref = "main"          # as a bug or a hostile actor would
    ok, messages = push.apply(plan, repo)

    assert not ok and "refusing to force-push" in messages[0]
    assert not fake.calls, "nothing may be published after the guard refuses"
    assert not git(repo, "ls-remote", "--heads", "origin").strip(), (
        "the guard must refuse BEFORE writing to the remote")


def test_apply_reports_a_missing_gh_rather_than_half_publishing(
        repo_with_remote: Path, monkeypatch):
    repo = repo_with_remote
    commit(repo, "a.py", "def a():\n    return 1\n", "first")
    monkeypatch.setattr(push, "gh_available",
                        lambda repo=None: (False, "gh is not on PATH"))
    plan = push.plan(ACTOR, repo, trunk="trunk", query_github=False)
    ok, messages = push.apply(plan, repo)
    assert not ok and "gh is not on PATH" in messages[0]
    assert not git(repo, "ls-remote", "--heads", "origin").strip(), (
        "nothing may be pushed when the PR half cannot run — a ref with no PR "
        "is invisible work")


def test_pr_wait_exits_124_on_timeout_not_1(monkeypatch, capsys):
    """0 = it happened, 124 = I gave up. Returning 1 for both conflates a slow
    merge with a rejected one, which is exactly what a script must distinguish."""
    from awgit import cli

    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, '{"state":"OPEN"}', ""))
    args = type("A", (), {"pr_cmd": "wait", "number": 5, "for_state": "merged",
                          "timeout": 0, "interval": 0})()
    assert cli._cmd_pr_wait(args) == 124


def test_pr_wait_exits_0_when_merged(monkeypatch):
    from awgit import cli

    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, '{"state":"MERGED"}', ""))
    args = type("A", (), {"pr_cmd": "wait", "number": 5, "for_state": "merged",
                          "timeout": 30, "interval": 0})()
    assert cli._cmd_pr_wait(args) == 0
