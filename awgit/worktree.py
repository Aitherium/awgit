"""Worktrees — the answer the rewrite guard points you at.

When another actor is live in your checkout, the fix is not to force the
rewrite, it is to get a checkout of your own. ``git worktree`` already does
that; this wraps it so the refusal can end in a command you can paste, and so
the new tree is created somewhere predictable.

Thin on purpose. Everything here is a ``git worktree`` invocation with a
default path and a readable error — there is no state of our own to keep, and a
worktree registry that could disagree with git's would be worse than none.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

#: Where a new worktree lands unless told otherwise. Inside the repo (and
#: gitignored by convention) so it travels with the checkout and is obvious to
#: find, rather than in a temp dir nobody can locate afterwards.
DEFAULT_PARENT = ".worktrees"


def _git(repo: Optional[Path], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo) if repo else None, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )


def main_root(cwd: Optional[Path] = None) -> Optional[Path]:
    """The MAIN worktree's root, even when called from inside a linked one.

    ``--show-toplevel`` answers "this worktree"; new worktrees must be created
    relative to the main one, or a worktree made from inside a worktree nests
    and the paths stop being predictable.
    """
    proc = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    common = Path(proc.stdout.strip())
    return common.parent if common.name == ".git" else common


def listing(cwd: Optional[Path] = None) -> List[Tuple[str, str, str]]:
    """(path, sha, branch) for every worktree git knows about."""
    proc = _git(cwd, "worktree", "list", "--porcelain")
    if proc.returncode != 0:
        return []
    out: List[Tuple[str, str, str]] = []
    path = sha = branch = ""
    for line in proc.stdout.splitlines() + [""]:
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("HEAD "):
            sha = line[len("HEAD "):]
        elif line.startswith("branch "):
            branch = line[len("branch "):].replace("refs/heads/", "")
        elif not line.strip() and path:
            out.append((path, sha, branch or "(detached)"))
            path = sha = branch = ""
    return out


def create(name: str, cwd: Optional[Path] = None,
           at: str = "HEAD") -> Tuple[bool, str, Optional[Path]]:
    """Make a worktree named ``name``. Returns (ok, message, path)."""
    root = main_root(cwd)
    if root is None:
        return False, "not a git worktree", None
    target = root / DEFAULT_PARENT / name
    if target.exists():
        return False, f"{target} already exists", target
    proc = _git(cwd, "worktree", "add", "-b", name, str(target), at)
    if proc.returncode != 0:
        # git's own message is the useful one (branch exists, dirty index, ...);
        # replacing it with our own would lose the diagnosis.
        return False, (proc.stderr or proc.stdout).strip(), target
    return True, f"created {target} on branch {name}", target


def remove(name_or_path: str, cwd: Optional[Path] = None,
           force: bool = False) -> Tuple[bool, str]:
    """Remove a worktree. Never forces unless asked — it can discard work."""
    root = main_root(cwd)
    candidate = Path(name_or_path)
    if not candidate.is_absolute() and root is not None:
        guess = root / DEFAULT_PARENT / name_or_path
        if guess.exists():
            candidate = guess
    args = ["worktree", "remove"] + (["--force"] if force else []) + [str(candidate)]
    proc = _git(cwd, *args)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()
    return True, f"removed {candidate}"
