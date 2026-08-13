"""A Change-Id must survive every operation that changes a commit's sha.

This is the whole premise of the trailer. `push` asks "is there already a PR
for this change?"; if the answer is keyed on sha it is NO after every amend,
and revising a review comment opens a second pull request instead of adding a
revision to the first.

git preserves a commit MESSAGE across amend, rebase and cherry-pick, so a
trailer survives them for free — but "for free" is a claim about git's
behaviour, not about ours, and the part that is ours is easy to get wrong: a
prepare-commit-msg hook that is not idempotent stamps a SECOND id on every
amend, and then the change has two identities and matches neither.

So each rewrite is exercised for real against a temp repo with the hooks
actually installed, not simulated by calling add_to_message twice.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit import changeid  # noqa: E402
from awgit.bridge import install_hooks  # noqa: E402


def _env() -> dict:
    """Run the scratch repo with the lease gate OFF.

    The gate guards the real shared worktree against one session clobbering
    another's uncommitted work. A throwaway repo has no peers, and inheriting
    ``VCS_LEASES_ENFORCE=1`` from the developer's shell makes every commit here
    fail with "no active lease covering: a.py" — a test outcome that depends on
    who is running it, which is worse than no test.
    """
    import os

    env = dict(os.environ)
    env["VCS_LEASES_ENFORCE"] = "0"
    return env


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=_env())
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "T")
    git(r, "config", "commit.gpgsign", "false")
    install_hooks(str(r))
    return r


def commit(repo: Path, name: str, body: str, message: str) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def test_the_hook_stamps_a_change_id(repo: Path):
    commit(repo, "a.py", "def a():\n    return 1\n", "add a")
    got = changeid.of_commit("HEAD", repo)
    assert got and changeid.is_valid(got), (
        f"prepare-commit-msg did not stamp a valid Change-Id (got {got!r}). "
        f"Message was:\n{git(repo, 'log', '-1', '--format=%B')}"
    )


def test_it_survives_amend_and_is_not_duplicated(repo: Path):
    """THE regression. Amend is how a review comment gets addressed."""
    sha1 = commit(repo, "a.py", "def a():\n    return 1\n", "add a")
    before = changeid.of_commit("HEAD", repo)

    (repo / "a.py").write_text("def a():\n    return 2\n", encoding="utf-8")
    git(repo, "add", "a.py")
    git(repo, "commit", "-q", "--amend", "--no-edit")
    sha2 = git(repo, "rev-parse", "HEAD").strip()

    assert sha1 != sha2, "the amend did not actually rewrite the commit"
    assert changeid.of_commit("HEAD", repo) == before, (
        "the Change-Id changed across an amend — push would open a SECOND pull "
        "request for the same change instead of adding a revision"
    )
    body = git(repo, "log", "-1", "--format=%B")
    assert body.count(changeid.TRAILER) == 1, (
        f"the hook stamped a second Change-Id on amend; the change now has two "
        f"identities and matches neither:\n{body}"
    )


def test_it_survives_a_rebase(repo: Path):
    commit(repo, "base.py", "x = 1\n", "base")
    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, "f.py", "def f():\n    return 1\n", "add f")
    before = changeid.of_commit("HEAD", repo)

    git(repo, "checkout", "-q", "main")
    commit(repo, "other.py", "y = 2\n", "other")
    git(repo, "checkout", "-q", "feature")
    git(repo, "rebase", "-q", "main")

    assert changeid.of_commit("HEAD", repo) == before, (
        "the Change-Id changed across a rebase — every restack would orphan its PR"
    )


def test_it_survives_a_cherry_pick(repo: Path):
    commit(repo, "base.py", "x = 1\n", "base")
    git(repo, "checkout", "-q", "-b", "feature")
    picked = commit(repo, "f.py", "def f():\n    return 1\n", "add f")
    before = changeid.of_commit("HEAD", repo)

    git(repo, "checkout", "-q", "main")
    git(repo, "cherry-pick", picked)
    assert changeid.of_commit("HEAD", repo) == before


def test_find_locates_the_commit_by_id(repo: Path):
    commit(repo, "a.py", "def a():\n    return 1\n", "add a")
    cid = changeid.of_commit("HEAD", repo)
    found = changeid.find(cid, repo)
    assert git(repo, "rev-parse", "HEAD").strip() in found


def test_a_merge_commit_is_not_stamped(repo: Path):
    """A merge is not one reviewable change and maps to no PR."""
    commit(repo, "base.py", "x = 1\n", "base")
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "s.py", "s = 1\n", "side change")
    git(repo, "checkout", "-q", "main")
    commit(repo, "m.py", "m = 1\n", "main change")
    git(repo, "merge", "--no-ff", "-q", "side", "-m", "merge side")

    body = git(repo, "log", "-1", "--format=%B")
    assert changeid.TRAILER not in body, (
        f"a merge commit was stamped with a Change-Id:\n{body}"
    )


def test_two_commits_get_different_ids(repo: Path):
    """A content hash would collapse a cherry-pick into one identity."""
    commit(repo, "a.py", "def a():\n    return 1\n", "same message")
    first = changeid.of_commit("HEAD", repo)
    commit(repo, "b.py", "def b():\n    return 1\n", "same message")
    assert changeid.of_commit("HEAD", repo) != first
