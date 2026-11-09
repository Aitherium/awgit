"""`awgit worktree new` never creates a worktree inside ANOTHER repository.

The worktree settings file is host-wide and lists absolute roots; on the
owner's host the first is ``C:/AitherOS-Fresh/.claude/worktrees``. Measured
2026-11-08: ``worktree new cliwt`` run in a pytest tmp repo created
``C:/AitherOS-Fresh/.claude/worktrees/cliwt`` -- inside the real shared
worktree, with a gitdir pointing back at the tmp repo -- twice.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit import cli, wtlife  # noqa: E402
from awgit import worktree as wt  # noqa: E402


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "commit.gpgsign", "false")
    (path / "f.txt").write_text("x\n", encoding="utf-8")
    git(path, "add", "f.txt")
    git(path, "-c", "user.email=t@example.invalid", "-c", "user.name=t",
        "commit", "-q", "--no-verify", "-m", "root")
    return path


@pytest.fixture
def two_repos(tmp_path: Path, monkeypatch):
    mine = make_repo(tmp_path / "mine")
    other = make_repo(tmp_path / "other")  # stands in for the real shared worktree
    foreign_root = other / ".claude" / "worktrees"
    foreign_root.mkdir(parents=True)
    plain = tmp_path / "plain-wt"  # inside no checkout, like D:/wt
    plain.mkdir()
    settings = tmp_path / "worktrees.json"
    monkeypatch.setenv(wtlife.SETTINGS_ENV, str(settings))
    monkeypatch.chdir(mine)
    return mine, other, foreign_root, plain, settings


def write_roots(settings: Path, roots) -> None:
    settings.write_text(json.dumps({"roots": [str(r) for r in roots]}), encoding="utf-8")


def test_a_root_inside_another_repo_is_never_used(two_repos):
    mine, other, foreign_root, _plain, settings = two_repos
    write_roots(settings, [foreign_root])
    assert cli.main(["worktree", "new", "cliwt", "--at", "HEAD"]) == 0
    assert not (foreign_root / "cliwt").exists(), "worktree created inside another repo"
    assert list(foreign_root.iterdir()) == []
    landed = mine / wt.DEFAULT_PARENT / "cliwt"
    assert landed.is_dir()
    assert (landed / ".git").is_file()
    # and git in the other repo knows nothing about it
    assert "cliwt" not in git(other, "worktree", "list")


def test_the_same_root_is_used_from_its_own_repo(two_repos):
    _mine, other, foreign_root, _plain, settings = two_repos
    write_roots(settings, [foreign_root])
    assert wtlife.resolve_roots(wtlife.load_settings(), wt.main_root(other)) == [foreign_root]


def test_a_root_inside_no_checkout_stays_usable(two_repos):
    mine, _other, foreign_root, plain, settings = two_repos
    write_roots(settings, [foreign_root, plain])
    assert wtlife.resolve_roots(wtlife.load_settings(), wt.main_root(mine)) == [plain]
    assert cli.main(["worktree", "new", "wt2", "--at", "HEAD"]) == 0
    assert (plain / "wt2").is_dir()
    assert not (foreign_root / "wt2").exists()


def test_status_and_adopt_see_only_this_repos_roots(two_repos, capsys):
    mine, other, foreign_root, _plain, settings = two_repos
    git(other, "worktree", "add", "-q", "-b", "theirs", str(foreign_root / "theirs"))
    write_roots(settings, [foreign_root])
    assert cli.main(["worktree", "adopt"]) == 0
    out = capsys.readouterr().out
    assert "adopted 0 worktree(s)" in out
    assert str(foreign_root) not in out
    assert (foreign_root / "theirs").is_dir()


def test_the_suite_never_reads_the_host_settings(monkeypatch):
    # conftest points AWGIT_WT_SETTINGS at a per-test temp file
    assert Path.home() / ".aither" not in wtlife.settings_path().parents
