"""Amending a middle commit must not silently drop the rest of the stack.

This is the single most common stacked-diff operation: a reviewer comments on
the middle PR, you go there, fix it, and amend. git leaves everything above
pointing at the commit you replaced, and does not follow.

Found by dogfooding, not by reasoning: during an end-to-end push test `prev` +
amend + `restack` printed "HEAD is up to date" and the third commit was gone.
It was noticed only because someone asked why the test kept reaching for raw
git instead of awgit — the reaching WAS the symptom.

The second test is the more important one. The first repair attempt replayed
every unreachable commit in the reflog, which included work from an abandoned
earlier stack. Resurrecting discarded commits is far worse than leaving an
orphan: an orphan is visible, and a resurrected commit looks like it belongs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit import stack as stackmod  # noqa: E402
from awgit.bridge import install_hooks  # noqa: E402


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
    git(r, "config", "user.email", "t@e.com")
    git(r, "config", "user.name", "T")
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "vcs"))
    monkeypatch.setenv("VCS_LEASES_ENFORCE", "0")
    install_hooks(str(r))
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


def test_amending_the_middle_orphans_the_top_and_restack_finds_it(repo: Path):
    commit(repo, "a.py", "def a():\n    return 1\n", "first")
    middle = commit(repo, "b.py", "def b():\n    return 2\n", "second")
    commit(repo, "c.py", "def c():\n    return 3\n", "third")
    before = [e.change_id for e in stackmod.load(repo, "trunk")]
    assert len(before) == 3

    # Go to the middle and amend it, exactly as answering review does.
    git(repo, "checkout", "-q", middle)
    (repo / "b.py").write_text("def b():\n    return 22\n", encoding="utf-8")
    git(repo, "add", "b.py")
    git(repo, "commit", "-q", "--amend", "--no-edit")

    assert len(stackmod.load(repo, "trunk")) == 2, "the amend should orphan the top"

    stranded = stackmod.orphans(repo, "trunk")
    assert [e.subject for e in stranded] == ["third"], (
        f"restack must find exactly the orphaned commit, got "
        f"{[e.subject for e in stranded]}")
    assert stranded[0].change_id == before[2], "the orphan keeps its Change-Id"


def test_it_does_not_resurrect_deliberately_discarded_work(repo: Path):
    """The bug the first version had, and the worse of the two failures."""
    commit(repo, "a.py", "def a():\n    return 1\n", "first")
    commit(repo, "junk.py", "def junk():\n    return 0\n", "ABANDONED work")
    git(repo, "reset", "-q", "--hard", "HEAD~1")   # deliberately thrown away

    middle = commit(repo, "b.py", "def b():\n    return 2\n", "second")
    commit(repo, "c.py", "def c():\n    return 3\n", "third")
    git(repo, "checkout", "-q", middle)
    (repo / "b.py").write_text("def b():\n    return 22\n", encoding="utf-8")
    git(repo, "add", "b.py")
    git(repo, "commit", "-q", "--amend", "--no-edit")

    subjects = [e.subject for e in stackmod.orphans(repo, "trunk")]
    assert "ABANDONED work" not in subjects, (
        "restack tried to resurrect a commit that was deliberately discarded — "
        "an orphan left behind is visible, a resurrected commit is not")
    assert subjects == ["third"]


def test_a_clean_stack_has_no_orphans(repo: Path):
    """The negative direction: nothing to repair must mean nothing replayed."""
    commit(repo, "a.py", "def a():\n    return 1\n", "first")
    commit(repo, "b.py", "def b():\n    return 2\n", "second")
    assert stackmod.orphans(repo, "trunk") == []
