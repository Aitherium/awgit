"""Worktree lifecycle: new -> dirty -> done (snapshot + remove + ledger) -> restore.

Pins the promises `awgit wt done` makes: unpushed work is never lost (a verified
reclaim ref holds tracked edits, staged work, untracked and non-build ignored
files), a secret-shaped file refuses the retirement, a junction/symlink target
survives removal, and `restore` brings the work back as uncommitted changes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit import worktree, wtlife  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                          encoding="utf-8", check=False,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "vcs"))
    monkeypatch.setenv(wtlife.SETTINGS_ENV, str(tmp_path / "settings.json"))
    path = tmp_path / "repo"
    (path / "src").mkdir(parents=True)
    (path / "src" / "a.txt").write_text("a\n", encoding="utf-8")
    (path / ".gitignore").write_text("*.png\n.env\n", encoding="utf-8")
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    monkeypatch.chdir(tmp_path)
    return path


def _make(repo: Path, name: str) -> Path:
    ok, msg, target = worktree.create(name, cwd=repo, at="HEAD")
    assert ok, msg
    assert target is not None
    wtlife.write_row(wtlife.new_row(target, name=name, purpose="test"))
    return target


def test_done_snapshots_dirty_work_and_restore_brings_it_back(repo: Path) -> None:
    tree = _make(repo, "agent")
    (tree / "src" / "a.txt").write_text("a\nedited\n", encoding="utf-8")
    (tree / "src" / "new.py").write_text("x = 1\n", encoding="utf-8")
    (tree / "src" / "staged.txt").write_text("s\n", encoding="utf-8")
    _git(tree, "add", "src/staged.txt")
    (tree / "shot.png").write_bytes(b"\x89PNG")
    ok, msg = wtlife.done(tree, ignored_work=["shot.png"])
    assert ok, msg
    assert not tree.exists()
    entry = wtlife.ledger("agent")[-1]
    assert entry["ref"] == "refs/heads/reclaim/agent"
    names = _git(repo, "show", "--name-only", "--format=", entry["ref"]).stdout.split()
    assert {"src/a.txt", "src/new.py", "src/staged.txt", "shot.png"} <= set(names)
    assert wtlife.read_row(tree) is None

    ok, msg = wtlife.restore("agent", cwd=repo)
    assert ok, msg
    assert (tree / "src" / "a.txt").read_text(encoding="utf-8") == "a\nedited\n"
    assert (tree / "src" / "new.py").exists()
    assert (tree / "shot.png").exists()
    assert _git(tree, "rev-parse", "HEAD").stdout.strip() == entry["head"]


def test_clean_tree_retires_without_a_snapshot(repo: Path) -> None:
    tree = _make(repo, "clean")
    ok, msg = wtlife.done(tree)
    assert ok, msg
    assert wtlife.ledger("clean")[-1]["ref"] == "refs/heads/clean"
    assert _git(repo, "rev-parse", "--verify", "-q", "refs/heads/reclaim/clean").returncode != 0


def test_secret_shaped_file_refuses_retirement(repo: Path) -> None:
    tree = _make(repo, "secret")
    (tree / ".env").write_text("K=v\n", encoding="utf-8")
    ok, msg = wtlife.done(tree, ignored_work=[".env"])
    assert not ok and "secret-shaped" in msg
    assert tree.exists()


def test_link_target_survives_removal(repo: Path, tmp_path: Path) -> None:
    tree = _make(repo, "linked")
    outside = tmp_path / "shared"
    outside.mkdir()
    (outside / "keep.txt").write_text("k\n", encoding="utf-8")
    link = tree / "mods"
    try:
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                           check=True, capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.symlink(outside, link)
    except (OSError, subprocess.CalledProcessError):
        pytest.fail("could not create a junction/symlink for the test")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("mods\n", encoding="utf-8")
    ok, msg = wtlife.done(tree)
    assert ok, msg
    assert (outside / "keep.txt").exists()


def test_adopt_registers_unregistered_trees(repo: Path) -> None:
    ok, msg, target = worktree.create("stray", cwd=repo, at="HEAD")
    assert ok, msg
    assert wtlife.read_row(target) is None
    got = wtlife.adopt(None, cwd=repo, everything=True)
    assert str(target) in got or any(Path(g) == target for g in got)
    assert (wtlife.read_row(target) or {}).get("origin") == "adopted"


def test_idle_threshold_drops_under_drive_floor(repo: Path) -> None:
    s = dict(wtlife.DEFAULT_SETTINGS, pressure_free_gb=1e12)
    assert wtlife.idle_threshold(repo, s)[0] == s["pressure_idle_hours"]
    s = dict(wtlife.DEFAULT_SETTINGS, pressure_free_gb=0.0)
    assert wtlife.idle_threshold(repo, s)[0] == s["idle_hours"]


def test_sparse_tree_with_a_file_outside_the_cone_snapshots(repo: Path) -> None:
    (repo / "other").mkdir()
    (repo / "other" / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "other")
    ok, msg, tree = worktree.create("sparse", cwd=repo, at="HEAD", sparse=["src"])
    assert ok and tree is not None, msg
    assert not (tree / "other").exists()
    (tree / "other").mkdir()
    (tree / "other" / "b.txt").write_text("b\nedited outside the cone\n", encoding="utf-8")
    (tree / "src" / "a.txt").write_text("a\nedited\n", encoding="utf-8")
    ok, msg = wtlife.done(tree)
    assert ok, msg
    ref = wtlife.ledger("sparse")[-1]["ref"]
    rows = [ln for ln in _git(repo, "diff", "--name-status", "HEAD", ref).stdout.splitlines()
            if ln.strip()]
    assert sorted(ln.split("\t")[-1] for ln in rows) == ["other/b.txt", "src/a.txt"]
    assert not any(ln.startswith("D") for ln in rows)
