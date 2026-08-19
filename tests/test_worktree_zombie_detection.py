"""A worktree removal that fails partway must never look like an ordinary failure.

`git worktree remove` unregisters the worktree (deletes its `.git` pointer,
drops the admin entry) BEFORE deleting the working directory's contents. If
the second half fails — a locked file, a shell still sitting inside the
directory as cwd, a Windows permission error — the worktree is left
unregistered and pointerless while its directory still exists on disk. Any
`git` command later run with that directory as cwd finds no local `.git`,
walks up, and silently resolves to whatever repo happens to contain it.

This is not hypothetical: it is exactly the shape that let a `git reset
--hard` believed to be scoped to an isolated worktree run against the
shared main repo instead, orphaning real commits from the branch pointer.

`remove()` must distinguish this ("PARTIAL REMOVAL" — a zombie was left) from
an ordinary failure (the worktree is untouched, still registered) and say so
loudly, never folding the two into one generic error string.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit import worktree  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=False,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "init")
    return path


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return _init_repo(tmp_path / "repo")


def test_ordinary_removal_succeeds(repo: Path) -> None:
    ok, msg, _path = worktree.create("wt-a", cwd=repo)
    assert ok, msg
    ok, msg = worktree.remove("wt-a", cwd=repo)
    assert ok, msg
    assert not (repo / worktree.DEFAULT_PARENT / "wt-a").exists()


def test_ordinary_failure_is_not_reported_as_a_zombie(repo: Path) -> None:
    """Removing a worktree that was never created: a plain failure, worktree
    untouched (there is nothing to touch) — must NOT be misreported as a
    partial removal, which would send someone hunting for a directory that
    was never dangerous.
    """
    ok, msg = worktree.remove("does-not-exist", cwd=repo)
    assert not ok
    assert "PARTIAL REMOVAL" not in msg
    assert "ZOMBIE" not in msg


def test_zombie_left_by_a_failed_removal_is_detected_and_named(repo: Path) -> None:
    """Reproduce the exact incident shape directly: unregister the worktree
    (as `git worktree remove` does in its first phase) and simulate the
    second phase failing by leaving the directory behind with real content —
    without going through git's own remove at all, since the point is to
    prove the DETECTION works regardless of how the zombie state arose.
    """
    ok, msg, _path = worktree.create("wt-zombie", cwd=repo)
    assert ok, msg
    target = repo / worktree.DEFAULT_PARENT / "wt-zombie"
    assert target.is_dir()

    # Phase one of a real `git worktree remove`: unregister + drop the
    # pointer. `git worktree remove` refuses on a dirty/locked directory
    # before this point in real life; here we reproduce the LEFTOVER state
    # directly, which is what `_is_zombie` must recognise regardless of how
    # it was produced.
    (target / ".git").unlink()
    common_dir_proc = _git(repo, "rev-parse", "--git-common-dir")
    common_dir = (repo / common_dir_proc.stdout.strip()).resolve()
    admin_entry = common_dir / "worktrees" / "wt-zombie"
    if admin_entry.is_dir():
        for f in admin_entry.rglob("*"):
            if f.is_file():
                f.unlink()
        for d in sorted(admin_entry.rglob("*"), reverse=True):
            if d.is_dir():
                d.rmdir()
        admin_entry.rmdir()

    assert worktree._is_zombie(target, repo), (
        "the exact leftover shape (unregistered, no .git pointer, directory "
        "still present) was not recognised as a zombie"
    )

    # And a removal attempt against this already-zombified path must name it
    # as such rather than reporting an ordinary git error.
    ok, msg = worktree.remove(str(target), cwd=repo)
    assert not ok
    assert "PARTIAL REMOVAL" in msg
    assert "ZOMBIE" in msg
    assert "Do NOT cd into it" in msg


def test_registered_worktree_with_pointer_is_never_a_zombie(repo: Path) -> None:
    """A live, correctly-registered worktree must never be misdetected."""
    ok, msg, _path = worktree.create("wt-live", cwd=repo)
    assert ok, msg
    target = repo / worktree.DEFAULT_PARENT / "wt-live"
    assert not worktree._is_zombie(target, repo)
