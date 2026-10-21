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
from typing import Dict, List, Optional, Tuple

# prepare-commit-msg runs BEFORE pre-commit, which is the only order that works
# for the Change-Id: the trailer has to be in the message git is about to record,
# and post-commit is too late to change it.
_HOOKS = ("prepare-commit-msg", "pre-commit", "post-commit")
_PKG_HOOKS = Path(__file__).parent / "hooks"


def _fragments() -> Dict[str, Tuple[str, ...]]:
    """Every fragment shipped for each chained hook, read from the package tree.

    This used to be a hand-written allowlist, and that was the bug: the
    ``vcs-mass-delete-guard`` fragment shipped on 2026-08-10 but was never added
    to it, so ``awgit hooks install`` copied only ``vcs-lease-check`` into
    ``pre-commit.d`` and the guard ran nowhere. Deriving the set from the
    directory makes "shipped but never installed" unrepresentable. Fragments run
    in name order (``chain.sh`` globs them).
    """
    out: Dict[str, Tuple[str, ...]] = {}
    for hook in _HOOKS:
        d = _PKG_HOOKS / f"{hook}.d"
        out[hook] = tuple(sorted(f.name for f in d.iterdir() if f.is_file())) if d.is_dir() else ()
    return out


_FRAGMENTS = _fragments()
_MARKER = "# aither-vcs-chain"


def _git(repo_root: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: never flash a console
    return subprocess.run(
        ["git", *args], cwd=str(repo_root), capture_output=True, text=True,
        encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=30, **kw,
    )


def _git_dir(repo_root: Path) -> Path:
    out = _git(repo_root, "rev-parse", "--git-dir")
    if out.returncode != 0:
        raise subprocess.CalledProcessError(out.returncode, out.args, out.stdout, out.stderr)
    p = out.stdout.strip()
    return Path(p) if Path(p).is_absolute() else repo_root / p


def _hooks_dir(repo_root: Path) -> Path:
    """The directory git actually runs hooks from.

    ``<git-dir>/hooks`` is wrong in a linked worktree: git runs the COMMON dir's
    hooks, not ``.git/worktrees/<name>/hooks``, so an install from a worktree
    landed where nothing reads it. ``git rev-parse --git-path hooks`` is right
    for both.
    """
    out = _git(repo_root, "rev-parse", "--git-path", "hooks")
    if out.returncode != 0 or not out.stdout.strip():
        return _git_dir(repo_root) / "hooks"
    p = Path(out.stdout.strip())
    return p if p.is_absolute() else repo_root / p


def _ensure_exec(path: Path) -> None:
    if os.name != "nt":
        path.chmod(path.stat().st_mode | 0o755)


def _same(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes().replace(b"\r\n", b"\n") == b.read_bytes().replace(b"\r\n", b"\n")
    except OSError:
        return False


def ensure_hooks(repo_root: Optional[str] = None) -> Dict[str, object]:
    """Install or upgrade the chained hooks in one repo; idempotent. Returns a report.

    Per hook: a pre-existing foreign body (Git LFS, a secret scanner) moves to
    ``<hook>.org`` and keeps running first; an older chain shim is replaced; every
    shipped fragment is copied when missing or different. Fragments the package
    does not ship (a user's own) are left alone. Never destroys a hook body: when
    a foreign hook AND a different ``.org`` both exist, that hook is left as it is
    and reported as a conflict.

    ``status``: ``installed`` (first time), ``upgraded`` (something changed),
    ``current`` (nothing to do), ``conflict``, or ``skipped`` (``reason`` says why).
    """
    root = Path(repo_root or ".").resolve()
    changed: List[str] = []
    preserved: List[str] = []
    conflicts: List[str] = []
    report: Dict[str, object] = {"repo": str(root), "status": "current", "changed": changed,
                                 "preserved": preserved, "conflicts": conflicts}
    if _git(root, "rev-parse", "--show-toplevel").returncode != 0:
        report.update(status="skipped", reason="not a git work tree")
        return report
    custom = _git(root, "config", "--get", "core.hooksPath").stdout.strip()
    if custom:
        # A hooks dir another tool owns (husky, lefthook) is usually TRACKED;
        # writing ours into it would edit the repo. Say so instead of guessing.
        report.update(status="skipped",
                      reason=f"core.hooksPath={custom} is managed by another tool")
        return report
    hooks_dir = _hooks_dir(root)
    report["hooks_dir"] = str(hooks_dir)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    chain = _PKG_HOOKS / "chain.sh"
    fresh = not any((hooks_dir / h).is_file()
                    and _MARKER in (hooks_dir / h).read_text(encoding="utf-8", errors="replace")
                    for h in _HOOKS)
    for hook in _HOOKS:
        target = hooks_dir / hook
        original = hooks_dir / f"{hook}.org"
        if target.exists() and _MARKER not in target.read_text(encoding="utf-8", errors="replace"):
            if not original.exists():
                shutil.move(str(target), str(original))
                preserved.append(f"{hook}.org")
            elif not _same(target, original):
                conflicts.append(f"{hook}: a foreign hook AND a different {hook}.org both exist")
                continue
            # else: e.g. `git lfs install` rewrote the same body; .org already runs it
        if not target.exists() or not _same(target, chain):
            shutil.copyfile(chain, target)
            changed.append(hook)
        _ensure_exec(target)
        frag_dir = hooks_dir / f"{hook}.d"
        frag_dir.mkdir(parents=True, exist_ok=True)
        for frag_name in _FRAGMENTS[hook]:
            src = _PKG_HOOKS / f"{hook}.d" / frag_name
            dst = frag_dir / frag_name
            if not dst.exists() or not _same(src, dst):
                shutil.copyfile(src, dst)
                changed.append(f"{hook}.d/{frag_name}")
            _ensure_exec(dst)
    if conflicts:
        report["status"] = "conflict"
    elif changed:
        report["status"] = "installed" if fresh else "upgraded"
    report["lease_mode"] = _git(root, "config", "--get", "awgit.leases").stdout.strip() or "auto"
    return report


def install_hooks(repo_root: Optional[str] = None) -> List[str]:
    """Wrap existing hooks with the chaining shim (idempotent, reversible).

    Never overwrites a live custom hook: the existing body moves to
    ``<hook>.org`` and is sourced first by ``chain.sh``. See ``ensure_hooks``.
    """
    rep = ensure_hooks(repo_root)
    if rep["status"] == "skipped":
        return []
    hooks_dir = Path(str(rep["hooks_dir"]))
    bad = {c.split(":", 1)[0] for c in rep["conflicts"]}  # type: ignore[union-attr]
    return [str(hooks_dir / h) for h in _HOOKS if h not in bad]


_SCAN_SKIP = {"node_modules", "$RECYCLE.BIN", "System Volume Information", "Windows",
              "Program Files", "Program Files (x86)", "ProgramData", "AppData"}


def find_repos(roots: List[str], depth: int = 2) -> List[str]:
    """Git work trees under ``roots`` (a ``.git`` dir or file), up to ``depth`` levels down.

    Does not descend into a repo once found, nor into hidden dirs, ``node_modules``
    or OS folders. Linked worktrees are returned too; ``ensure_hooks`` resolves
    them to the shared hooks dir, and callers dedupe on ``hooks_dir``.
    """
    found: List[str] = []

    def walk(d: Path, level: int) -> None:
        if (d / ".git").exists():
            found.append(str(d))
            return
        if level >= depth:
            return
        try:
            kids = sorted(d.iterdir())
        except OSError:
            return
        for k in kids:
            try:
                if (k.is_dir() and not k.is_symlink() and not k.name.startswith(".")
                        and k.name not in _SCAN_SKIP):
                    walk(k, level + 1)
            except OSError:
                continue

    for r in roots:
        walk(Path(r), 0)
    return found


def uninstall_hooks(repo_root: Optional[str] = None) -> List[str]:
    """Remove chain hooks and restore ``<hook>.org`` bodies (reversible)."""
    root = Path(repo_root or ".")
    hooks_dir = _hooks_dir(root)
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
