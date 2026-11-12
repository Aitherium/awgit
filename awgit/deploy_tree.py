"""A deploy tree is served, not edited.

Some checkouts are LIVE: containers bind-mount them, an editable install runs
from them, a sync lane moves them to the branch tip after every merge. Editing
one by hand mixes two jobs in one directory, and the cost was measured on
2026-11-11: dozens of agent sessions edited such a tree in place, left 5,522
uncommitted files nobody owned, and 282 of them collided with merged work. The
sync lane refused to overwrite them (correctly) and the tree sat 2,753 paths
behind its branch for days, so nothing merged in that time was live.

The fix has two halves, and this module is the first:

* **Mark the tree** (``awgit deploy-tree mark``): sets ``awgit.deploytree=true``
  in that repo's git config. Nothing is hard-coded; any checkout can opt in.
* **Refuse agent edits into it** (``awgit deploy-tree guard``): a Claude Code
  PreToolUse hook for Edit / Write / MultiEdit / NotebookEdit. It reads the hook
  payload on stdin and denies a write whose path lies inside a marked tree,
  naming the fix (``awgit wt new <name>``). Everything else passes untouched.

The second half is the sync lane's rescue mode, which commits and pushes any
uncommitted content it would otherwise refuse on, so a stray edit can delay a
deploy by one run at most and is never lost.

Override for the one process that should write there by hand:
``AWGIT_ALLOW_DEPLOY_TREE_EDIT=1``, an environment variable rather than a flag
for the same reason as :mod:`awgit.guard`'s override: it is a decision someone
made once and that is visible in the environment.
"""

from __future__ import annotations

import configparser
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

OVERRIDE_ENV = "AWGIT_ALLOW_DEPLOY_TREE_EDIT"
CONFIG_KEY = "awgit.deploytree"
WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
#: Untracked scratch space a session may still use inside a deploy tree. Nothing the sync
#: moves lives there, so a write cannot block a deploy. Measured 2026-11-11: an unattended
#: session had no other writable place and stalled on the first refusal.
DEFAULT_ALLOW = (".scratch/",)

_cache: Dict[str, Optional[Path]] = {}


def _git_dir(root: Path) -> Optional[Path]:
    """The repo's git dir for a work tree root (a ``.git`` dir, or a ``gitdir:`` file)."""
    dot = root / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():
        try:
            line = dot.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if line.startswith("gitdir:"):
            p = Path(line[7:].strip())
            return p if p.is_absolute() else (root / p).resolve()
    return None


def _marked(git_dir: Path) -> bool:
    """True when the repo config sets awgit.deploytree. Parsed directly: this runs on
    every agent edit, and a ``git config`` subprocess per call costs ~50 ms on Windows.
    A linked worktree's config lives in its common dir, so both are read."""
    cands = [git_dir / "config"]
    common = git_dir / "commondir"
    if common.is_file():
        try:
            c = Path(common.read_text(encoding="utf-8").strip())
            cands.append((c if c.is_absolute() else git_dir / c).resolve() / "config")
        except OSError as exc:  # unreadable commondir: judge by this tree's own config
            print(f"awgit deploy-tree: cannot read {common}: {exc}", file=sys.stderr)
    for cfg in cands:
        if not cfg.is_file():
            continue
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            cp.read(cfg, encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError):
            continue
        val = cp.get("awgit", "deploytree", fallback="") if cp.has_section("awgit") else ""
        if val.strip().lower() in ("true", "yes", "on", "1"):
            return True
    return False


def deploy_tree_of(path: str | os.PathLike[str]) -> Optional[Path]:
    """The marked deploy tree that contains ``path``, or None.

    Only the NEAREST work tree counts: a linked worktree nested under a deploy tree
    (or anywhere else) is its own checkout and is never refused because of where it
    sits on disk. A linked worktree's own config is not consulted for the mark, only
    the main tree's: ``mark`` writes the repo config, which linked worktrees share,
    so they are told apart by being linked (a ``.git`` file, not a directory)."""
    p = Path(os.path.abspath(os.fspath(path)))
    for anc in [p, *p.parents]:
        key = str(anc).lower()
        if key in _cache:
            return _cache[key]
        gd = _git_dir(anc)
        if gd is None:
            continue
        hit = anc if ((anc / ".git").is_dir() and _marked(gd)) else None
        _cache[key] = hit
        return hit
    return None


def _target(tool_input: Dict[str, Any]) -> Optional[str]:
    for k in ("file_path", "notebook_path", "path"):
        v = tool_input.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _allowed(tree: Path, target: str) -> bool:
    """Scratch prefixes and gitignored paths pass: the sync never moves them."""
    try:
        rel = Path(os.path.abspath(target)).relative_to(tree).as_posix()
    except ValueError:
        return False
    if any(rel == a.rstrip("/") or rel.startswith(a) for a in DEFAULT_ALLOW):
        return True
    r = subprocess.run(["git", "-C", str(tree), "check-ignore", "-q", "--no-index", "--", rel],
                       capture_output=True)
    return r.returncode == 0


def decide(payload: Dict[str, Any], env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The refusal text for a PreToolUse payload, or None to let it through."""
    env = os.environ if env is None else env
    if env.get(OVERRIDE_ENV, "").strip() in ("1", "true", "yes"):
        return None
    if payload.get("tool_name") not in WRITE_TOOLS:
        return None
    target = _target(payload.get("tool_input") or {})
    if not target:
        return None
    if not os.path.isabs(target) and payload.get("cwd"):
        target = os.path.join(payload["cwd"], target)
    tree = deploy_tree_of(target)
    if tree is None:
        return None
    if _allowed(tree, target):
        return None
    return (
        f"{tree} is a deploy tree (awgit.deploytree=true): containers serve it and the sync "
        "lane moves it to the branch tip after every merge, so edits made here are never "
        "committed and block the next deploy. Make this change in a worktree instead: "
        "`awgit wt new <name>` (then commit + PR to develop; it goes live on merge). "
        f"Only a deliberate hand fix may set {OVERRIDE_ENV}=1."
    )


def guard_main(stdin: Any = None, stdout: Any = None) -> int:
    """PreToolUse hook entry. Always exits 0: a broken payload must never block a tool."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    try:
        payload = json.load(stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        reason = decide(payload)
    except Exception:  # noqa: BLE001 -- fail open: the guard is advice with teeth, not a crash
        return 0
    if reason:
        json.dump({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}, stdout)
    return 0


def mark(repo: str | os.PathLike[str], on: bool = True) -> str:
    root = Path(repo)
    if on:
        subprocess.run(["git", "-C", str(root), "config", CONFIG_KEY, "true"], check=True)
    else:
        subprocess.run(["git", "-C", str(root), "config", "--unset", CONFIG_KEY], check=False)
    _cache.clear()
    return f"{root}: {CONFIG_KEY}={'true' if on else '(unset)'}"


def status(repo: str | os.PathLike[str]) -> Dict[str, Any]:
    """Whether ``repo`` is marked, and how much uncommitted content sits in it by top dir."""
    root = Path(repo).resolve()
    out = subprocess.run(["git", "-C", str(root), "-c", "core.quotePath=false", "status",
                          "--porcelain=v1", "-uno", "--no-renames"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    by_top: Dict[str, int] = {}
    n = 0
    for line in out.stdout.splitlines():
        p = line[3:]
        if not p:
            continue
        n += 1
        top = p.split("/", 1)[0]
        by_top[top] = by_top.get(top, 0) + 1
    gd = _git_dir(root)
    return {
        "tree": str(root),
        "marked": bool(gd and _marked(gd)),
        "dirty": n,
        "by_top": dict(sorted(by_top.items(), key=lambda kv: -kv[1])[:10]),
    }
