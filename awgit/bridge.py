"""Git-bridge integration: hook chaining, install/uninstall, autosync guard.

The layer must CHAIN with the existing custom hooks in ``.git/hooks`` (this repo
has live ``pre-commit``, ``post-commit``, ``post-merge``, ``pre-push``), never
overwrite them. ``install_hooks`` wraps each hook with ``chain.sh`` which
sources the pre-existing body (moved to ``<hook>.org``) then runs the ``.d``
fragments. No ``post-merge`` / ``pre-push`` hook is added — merge/push semantics
stay byte-identical.

Data lives OUTSIDE the git tree, so a tree-copy sync or a CI checkout never
sees it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

_HOOKS = ("prepare-commit-msg", "pre-commit", "post-commit")
_FRAGMENTS = {
    # Runs BEFORE pre-commit, which is the only order that works: the trailer
    # has to be in the message git is about to record, and post-commit is too
    # late to change it.
    "prepare-commit-msg": ("awgit-change-id",),
    "pre-commit": ("vcs-lease-check",),
    "post-commit": ("vcs-capture",),
}
_MARKER = "# aither-vcs-chain"


def _git_dir(repo_root: Path) -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.strip()
    return Path(out) if Path(out).is_absolute() else repo_root / out


def _ensure_exec(path: Path) -> None:
    if os.name != "nt":
        path.chmod(path.stat().st_mode | 0o755)


def install_hooks(repo_root: Optional[str] = None) -> List[str]:
    """Wrap existing hooks with the chaining shim (idempotent, reversible).

    Never overwrites a live custom hook: the existing body moves to
    ``<hook>.org`` and is sourced first by ``chain.sh``.
    """
    root = Path(repo_root or ".")
    hooks_dir = _git_dir(root) / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    pkg_hooks = Path(__file__).parent / "hooks"
    installed: List[str] = []
    for hook in _HOOKS:
        target = hooks_dir / hook
        original = hooks_dir / f"{hook}.org"
        if target.exists():
            text = target.read_text(encoding="utf-8", errors="replace")
            if _MARKER not in text and not original.exists():
                shutil.move(str(target), str(original))
        shutil.copyfile(pkg_hooks / "chain.sh", target)
        _ensure_exec(target)
        frag_dir = hooks_dir / f"{hook}.d"
        frag_dir.mkdir(parents=True, exist_ok=True)
        for frag_name in _FRAGMENTS[hook]:
            src = pkg_hooks / f"{hook}.d" / frag_name
            if src.exists():
                dst = frag_dir / frag_name
                shutil.copyfile(src, dst)
                _ensure_exec(dst)
        installed.append(str(target))
    return installed


def uninstall_hooks(repo_root: Optional[str] = None) -> List[str]:
    """Remove chain hooks and restore ``<hook>.org`` bodies (reversible)."""
    root = Path(repo_root or ".")
    hooks_dir = _git_dir(root) / "hooks"
    removed: List[str] = []
    for hook in _HOOKS:
        target = hooks_dir / hook
        original = hooks_dir / f"{hook}.org"
        if target.exists():
            text = target.read_text(encoding="utf-8", errors="replace")
            if _MARKER in text:
                target.unlink()
        if original.exists():
            shutil.move(str(original), str(target))
        removed.append(str(target))
    return removed


def verify_deploy_tree(repo_root: Optional[str] = None) -> bool:
    """Tripwire for the clobber class: unmerged paths in the working tree.

    A cheap ``git status --porcelain`` check — unmerged markers mean a semantic
    merge is half-staged and a tree-copy sync must NOT propagate that state.
    """
    root = Path(repo_root or ".")
    out = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout
    for line in out.splitlines():
        xy = line[:2]
        if xy[0] == "U" or xy in ("AA", "DD"):
            return False
    return True
