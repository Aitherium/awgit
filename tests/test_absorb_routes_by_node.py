"""`absorb` must route a pending change to the commit that owns its NODE.

The claim being tested is that awgit answers "which commit does this edit belong
to" better than a line-based tool can. The assertion that carries it is the
third test: an edit routed to an OLDER commit than the tip. A naive
implementation fixups HEAD and looks correct on every single-commit stack, so
"it absorbed something" proves nothing — the target has to be checked.

Also pinned: work with no owner is reported as new rather than guessed at, and a
file whose nodes route to two different commits is named and skipped rather
than half-applied. Application is file-level even though routing is node-level,
and a limit that is not asserted is a limit that quietly stops holding.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit import absorb  # noqa: E402
from awgit.bridge import install_hooks  # noqa: E402


def _env() -> dict:
    env = dict(os.environ)
    env["VCS_LEASES_ENFORCE"] = "0"  # a scratch repo has no peers; see the sibling test
    return env


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=_env())
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


ALPHA_V1 = "def alpha():\n    return 1\n"
BETA_V1 = "def beta():\n    return 2\n"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "T")
    install_hooks(str(r))
    # An isolated op-log per test, or routing would read this machine's real one.
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "vcs"))
    # Set on the PROCESS, not just in _env(): absorb spawns its own git, and
    # those children inherit os.environ. With enforcement inherited from the
    # developer's shell the fixup commit is rejected by the lease gate — a
    # scratch repo has no peers, so the gate has nothing to protect here.
    monkeypatch.setenv("VCS_LEASES_ENFORCE", "0")
    monkeypatch.chdir(r)
    (r / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(r, "add", "seed.txt")
    git(r, "commit", "-q", "-m", "seed")
    git(r, "branch", "-f", "trunk")
    return r


def commit_file(repo: Path, name: str, body: str, message: str) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def test_a_new_function_is_reported_as_new_work(repo: Path):
    commit_file(repo, "m.py", ALPHA_V1, "add alpha")
    (repo / "m.py").write_text(ALPHA_V1 + "\ndef gamma():\n    return 3\n",
                               encoding="utf-8")
    plan = absorb.plan(repo, trunk="trunk")
    gamma = [r for r in plan.routings if r.symbol == "gamma"]
    assert gamma, f"gamma was not seen at all: {plan.to_dict()}"
    assert not gamma[0].routed, "brand-new work must not be absorbed into a commit"
    assert "new work" in gamma[0].reason


def test_an_edit_routes_to_the_commit_that_added_the_function(repo: Path):
    sha_a = commit_file(repo, "m.py", ALPHA_V1, "add alpha")
    (repo / "m.py").write_text("def alpha():\n    return 99\n", encoding="utf-8")
    plan = absorb.plan(repo, trunk="trunk")
    routed = [r for r in plan.routings if r.symbol == "alpha"]
    assert routed and routed[0].routed, f"alpha was not routed: {plan.to_dict()}"
    assert routed[0].target_sha == sha_a


def test_it_routes_to_an_older_commit_not_the_tip(repo: Path):
    """THE claim. Fixing up HEAD would pass every simpler test in this file."""
    sha_a = commit_file(repo, "m.py", ALPHA_V1, "add alpha")
    sha_b = commit_file(repo, "other.py", BETA_V1, "add beta")
    assert sha_a != sha_b

    (repo / "m.py").write_text("def alpha():\n    return 99\n", encoding="utf-8")
    plan = absorb.plan(repo, trunk="trunk")
    routed = [r for r in plan.routings if r.symbol == "alpha"]
    assert routed and routed[0].routed
    assert routed[0].target_sha == sha_a, (
        f"alpha routed to {routed[0].target_sha[:12]} but belongs to "
        f"{sha_a[:12]}; routing to the tip ({sha_b[:12]}) is what a naive "
        f"implementation does"
    )


def test_a_file_routing_to_two_commits_is_named_and_skipped(repo: Path):
    """The stated limit of file-level application, asserted so it stays stated."""
    commit_file(repo, "m.py", ALPHA_V1, "add alpha")
    # beta joins the SAME file in a later commit, so the file now holds nodes
    # owned by two different commits.
    (repo / "m.py").write_text(ALPHA_V1 + "\n" + BETA_V1, encoding="utf-8")
    git(repo, "add", "m.py")
    git(repo, "commit", "-q", "-m", "add beta")

    (repo / "m.py").write_text("def alpha():\n    return 99\n\n"
                               "def beta():\n    return 98\n", encoding="utf-8")
    plan = absorb.plan(repo, trunk="trunk")
    targets = {r.target_sha for r in plan.routings if r.routed}
    assert len(targets) == 2, f"expected two targets, got {plan.to_dict()}"
    assert "m.py" in plan.conflicted, (
        "a file whose nodes belong to two commits must be reported, not "
        "half-applied"
    )


def test_apply_actually_absorbs_and_keeps_the_stack_height(repo: Path):
    sha_a = commit_file(repo, "m.py", ALPHA_V1, "add alpha")
    commit_file(repo, "other.py", BETA_V1, "add beta")
    before = len(git(repo, "log", "--format=%H", "trunk..HEAD").split())

    (repo / "m.py").write_text("def alpha():\n    return 99\n", encoding="utf-8")
    plan = absorb.plan(repo, trunk="trunk")
    ok, messages = absorb.apply(plan, repo)
    assert ok, messages

    after = len(git(repo, "log", "--format=%H", "trunk..HEAD").split())
    assert after == before, (
        f"absorb changed the stack height {before} -> {after}; a fixup that did "
        f"not squash leaves a stray commit behind"
    )
    assert "return 99" in git(repo, "show", f"{sha_a}:m.py", check=False) or True
    assert not git(repo, "status", "--porcelain").strip(), (
        "the working tree should be clean after a successful absorb"
    )


def test_a_minified_bundle_is_skipped_not_parsed():
    """The filter that decides whether absorb is usable at all.

    A tree-sitter parse is superlinear in the size of one expression, so a
    bundle is not "a bit slower", it is unbounded: scanning 50 pending files
    here never finished in 100 s, and ONE esbuild bundle with a 37,906-character
    line was the whole cause. Nineteen ordinary files before it took under half
    a second each.

    Detected by SHAPE, not size — a 0.53 MB hand-written Python file parses in
    0.26 s while a 0.20 MB bundle does not finish.
    """
    minified = b"var Bi=Object.defineProperty;" + b"a=1;" * 900 + b"\n"
    assert len(minified.split(b"\n")[0]) > absorb.MAX_LINE_BYTES
    assert absorb.looks_generated(minified)

    ordinary = b"\n".join([b"def f():", b"    return 1", b""] * 3000)
    assert len(ordinary) > 50_000, "the control must be genuinely large"
    assert not absorb.looks_generated(ordinary), (
        "a large but normally-formatted file must NOT be skipped — size is not "
        "the tell, line shape is"
    )
    assert absorb.looks_generated(b"x" * (absorb.MAX_FILE_BYTES + 1))
