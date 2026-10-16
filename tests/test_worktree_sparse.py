"""`awgit worktree new --sparse` makes a cheap tree that is still safe to commit in.

A sparse agent worktree is made with ``--no-checkout`` so git does not write the
whole tree first. That shape is dangerous: a ``--no-checkout`` worktree has an
EMPTY index, and a commit made there records every tracked file as deleted.
These tests pin the three things the feature promises — only the named
directories on disk, the MAIN worktree's sparse settings untouched, and an index
that is populated and equal to HEAD — plus the rollback when the index check
fails, and that ``worktree reap`` delegates to the repo's reaper.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit import cli, worktree  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    for rel in ("keep/a.txt", "keep/deep/b.txt", "other/c.txt", "third/d.txt", "top.txt"):
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(rel + "\n", encoding="utf-8")
    _git(path, "init", "-q")
    _git(path, "add", "-A")
    _git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return path


def _main_sparse_state(repo: Path) -> tuple:
    return (
        _git(repo, "config", "--worktree", "--get", "core.sparseCheckout").stdout.strip(),
        _git(repo, "config", "--get", "core.sparseCheckout").stdout.strip(),
        (repo / ".git" / "info" / "sparse-checkout").exists(),
        # `ls-files -v` tags skip-worktree entries `S`: a main tree that went
        # sparse would show them here, and its files would vanish from disk.
        _git(repo, "ls-files", "-v").stdout,
        sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*.txt")
               if worktree.DEFAULT_PARENT not in p.parts),
    )


def test_sparse_tree_has_only_named_dirs_and_full_index(repo: Path) -> None:
    before = _main_sparse_state(repo)
    ok, msg, target = worktree.create("agent", cwd=repo, at="HEAD", sparse=["keep"])
    assert ok, msg
    assert target is not None
    on_disk = sorted(p.relative_to(target).as_posix() for p in target.rglob("*.txt"))
    # Cone mode always keeps root-level files; nothing from other dirs.
    assert on_disk == ["keep/a.txt", "keep/deep/b.txt", "top.txt"]
    assert not (target / "other").exists()
    assert not (target / "third").exists()

    # The index is the WHOLE tree (skip-worktree outside the cone) and equals HEAD.
    tracked = _git(target, "ls-files").stdout.split()
    assert len(tracked) == 5
    assert _git(target, "diff", "--cached", "--quiet", "HEAD").returncode == 0
    assert worktree.index_ok(target)[0]

    # The main worktree's sparse settings and files are untouched.
    assert _main_sparse_state(repo) == before
    assert _git(target, "config", "--worktree", "--get",
                "core.sparseCheckout").stdout.strip() == "true"


def test_commit_in_sparse_tree_deletes_nothing(repo: Path) -> None:
    ok, msg, target = worktree.create("agent2", cwd=repo, at="HEAD", sparse=["keep/"])
    assert ok, msg
    (target / "keep" / "a.txt").write_text("edited\n", encoding="utf-8")
    _git(target, "add", "keep/a.txt")
    _git(target, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "edit")
    changed = _git(target, "diff", "--name-status", "HEAD~1", "HEAD").stdout.split("\n")
    assert [c for c in changed if c.strip()] == ["M\tkeep/a.txt"]


def test_empty_index_is_refused_and_rolled_back(repo: Path, monkeypatch) -> None:
    # Simulate the failure the guard exists for: the populate step "succeeds"
    # but leaves the --no-checkout tree's index empty.
    real = worktree._git

    def fake(cwd, *args):
        if args[:1] == ("read-tree",):
            return subprocess.CompletedProcess(["git", *args], 0, "", "")
        return real(cwd, *args)

    monkeypatch.setattr(worktree, "_git", fake)
    ok, msg, target = worktree.create("broken", cwd=repo, at="HEAD", sparse=["keep"])
    assert not ok
    assert "EMPTY" in msg and "rolled back" in msg
    assert target is not None and not target.exists()
    assert "broken" not in _git(repo, "branch", "--list", "broken").stdout


def test_bad_sparse_dir_is_refused_before_git(repo: Path) -> None:
    ok, msg, _ = worktree.create("bad", cwd=repo, at="HEAD", sparse=["../escape"])
    assert not ok and "invalid --sparse" in msg
    assert not (repo / worktree.DEFAULT_PARENT / "bad").exists()


def test_non_sparse_create_unchanged(repo: Path) -> None:
    ok, msg, target = worktree.create("plain", cwd=repo, at="HEAD")
    assert ok, msg
    assert (target / "other" / "c.txt").exists()


def test_reap_delegates_to_repo_reaper(repo: Path, monkeypatch, capsys) -> None:
    tools = repo / "dev" / "tools"
    tools.mkdir(parents=True)
    out = repo / "argv.txt"
    (tools / worktree.REAP_TOOL_NAME).write_text(
        "import sys\nopen(%r, 'w').write(' '.join(sys.argv[1:]))\n" % str(out),
        encoding="utf-8")
    monkeypatch.delenv(worktree.REAP_TOOL_ENV, raising=False)
    monkeypatch.chdir(repo)
    assert cli.main(["worktree", "reap", "--older-than", "6", "--dry-run"]) == 0
    assert out.read_text(encoding="utf-8") == "--min-idle-hours 6.0"
    assert cli.main(["worktree", "reap", "--older-than", "6"]) == 0
    assert out.read_text(encoding="utf-8") == "--min-idle-hours 6.0 --reap"


def test_reap_without_tool_exits_2(repo: Path, monkeypatch) -> None:
    monkeypatch.delenv(worktree.REAP_TOOL_ENV, raising=False)
    monkeypatch.chdir(repo)
    assert cli.main(["worktree", "reap", "--dry-run"]) == 2


def test_cli_new_sparse(repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(repo)
    assert cli.main(["worktree", "new", "cliwt", "--at", "HEAD", "--sparse", "other"]) == 0
    target = repo / worktree.DEFAULT_PARENT / "cliwt"
    assert (target / "other" / "c.txt").exists()
    assert not (target / "keep").exists()


@pytest.mark.parametrize("bad", [
    "nope",                                   # no such directory
    "top.txt",                                # a file, not a directory
    ".",                                      # the root: a cone of nothing
    "C:/Program Files/Git/keep",              # Git Bash's MSYS form of /keep
    r"C:\keep",
])
def test_sparse_dir_must_be_a_tree_at_base(repo: Path, bad: str) -> None:
    ok, msg, _ = worktree.create("bad", cwd=repo, at="HEAD", sparse=[bad])
    assert not ok, msg
    assert not (repo / worktree.DEFAULT_PARENT / "bad").exists()
    assert "bad" not in _git(repo, "branch", "--list", "bad").stdout


def test_drive_letter_names_msys_conversion(repo: Path) -> None:
    ok, msg, _ = worktree.create("bad", cwd=repo, at="HEAD",
                                 sparse=["C:/Program Files/Git/keep"])
    assert not ok and "MSYS_NO_PATHCONV" in msg


def test_worktree_config_not_rewritten_when_already_true(repo: Path, monkeypatch) -> None:
    _git(repo, "config", "extensions.worktreeConfig", "true")
    real = worktree._git
    writes = []

    def spy(cwd, *args):
        if args[:1] == ("config",) and "extensions.worktreeConfig" in args \
                and "--get" not in args:
            writes.append(args)
        return real(cwd, *args)

    monkeypatch.setattr(worktree, "_git", spy)
    ok, msg, _ = worktree.create("agent3", cwd=repo, at="HEAD", sparse=["keep"])
    assert ok, msg
    assert writes == []


def test_worktree_config_written_when_unset(repo: Path) -> None:
    ok, msg, _ = worktree.create("agent4", cwd=repo, at="HEAD", sparse=["keep"])
    assert ok, msg
    assert _git(repo, "config", "--get",
                "extensions.worktreeConfig").stdout.strip() == "true"


def test_rollback_that_fails_names_what_was_left(repo: Path, monkeypatch) -> None:
    real = worktree._git

    def fake(cwd, *args):
        if args[:1] == ("read-tree",):
            return subprocess.CompletedProcess(["git", *args], 0, "", "")
        if args[:2] == ("worktree", "remove"):
            return subprocess.CompletedProcess(["git", *args], 1, "", "Permission denied")
        if args[:2] == ("branch", "-D"):
            return subprocess.CompletedProcess(["git", *args], 1, "", "checked out")
        return real(cwd, *args)

    monkeypatch.setattr(worktree, "_git", fake)
    ok, msg, target = worktree.create("stuck", cwd=repo, at="HEAD", sparse=["keep"])
    assert not ok
    assert "rolled back" not in msg
    assert "INCOMPLETE" in msg and str(target) in msg and "branch stuck" in msg


@pytest.mark.parametrize("hours", ["0", "-3", "nan", "x"])
def test_reap_rejects_non_positive_hours(repo: Path, monkeypatch, hours: str) -> None:
    tools = repo / "dev" / "tools"
    tools.mkdir(parents=True)
    out = repo / "argv.txt"
    (tools / worktree.REAP_TOOL_NAME).write_text(
        "import sys\nopen(%r, 'w').write(' '.join(sys.argv[1:]))\n" % str(out),
        encoding="utf-8")
    monkeypatch.delenv(worktree.REAP_TOOL_ENV, raising=False)
    monkeypatch.chdir(repo)
    with pytest.raises(SystemExit) as exc:
        cli.main(["worktree", "reap", "--older-than", hours])
    assert exc.value.code == 2
    assert not out.exists()


def test_sparse_help_says_put_it_last(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["worktree", "new", "--help"])
    assert "worktree new NAME --sparse a b" in " ".join(capsys.readouterr().out.split())
