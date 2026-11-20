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

A second, independent rule lives here too: the same hook refuses a loose
script (``.sh``/``.ps1``/``.cmd``/``.bat``/``.py``/``.js``/``.ts``) that no
git work tree owns -- authored outside every work tree, or parked in the
system temp dir. Hand-rolled automation nothing reviews, owns or can
reproduce is how "never hand-roll anything" gets broken five times in a week;
the fix is a platform lane (``awgit wt new`` in the owning repo, awrun, awsh,
media-forge, MCP), not a looser guard. Anything inside a work tree, plus
``.scratch/`` paths and data/artifact extensions (``.json``/``.txt``/``.md``/
``.mp4``/``.png`` ...), passes untouched.

Override for the one process that should write there by hand:
``AWGIT_ALLOW_DEPLOY_TREE_EDIT=1`` for a deploy-tree edit, and
``AWGIT_ALLOW_OUTSIDE_REPO=1`` for a loose script that truly has no repo.
Environment variables rather than flags for the same reason as
:mod:`awgit.guard`'s override: a decision someone made once, visible in the
environment.
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
#: A loose script outside every repo is refused unless the author says, in
#: the environment, that this one really has no repo. Same idiom as
#: :data:`OVERRIDE_ENV`: a visible, one-shot decision.
LOOSE_OVERRIDE_ENV = "AWGIT_ALLOW_OUTSIDE_REPO"
CONFIG_KEY = "awgit.deploytree"
WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
#: Suffixes that mean "this is automation somebody will point at a job". Data and
#: artifacts (.json/.txt/.md/.mp4/.png ...) are deliberately absent: writing a
#: fixture into temp is not hand-rolling a tool.
SCRIPT_SUFFIXES = (".sh", ".ps1", ".cmd", ".bat", ".py", ".js", ".ts")
#: Untracked scratch space a session may still use inside a deploy tree. Nothing the sync
#: moves lives there, so a write cannot block a deploy. Measured 2026-11-11: an unattended
#: session had no other writable place and stalled on the first refusal.
DEFAULT_ALLOW = (".scratch/",)

_cache: Dict[str, Optional[Path]] = {}
_wt_cache: Dict[str, Optional[Path]] = {}


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


def worktree_of(path: str | os.PathLike[str]) -> Optional[Path]:
    """The nearest git work tree root that contains ``path``, or None.

    A ``.git`` directory (main checkout) or file (linked worktree, submodule)
    both count: either way the path is under version control and may be
    committed and reviewed. Cached on the same string-key basis as
    :func:`deploy_tree_of` so a session's repeated edits cost one walk."""
    p = Path(os.path.abspath(os.fspath(path)))
    for anc in [p, *p.parents]:
        key = str(anc).lower()
        if key in _wt_cache:
            return _wt_cache[key]
        if _git_dir(anc) is not None:
            _wt_cache[key] = anc
            return anc
    _wt_cache[str(p).lower()] = None
    return None


def _under(child: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    """True when ``child`` is ``parent`` or lies beneath it, case-insensitively."""
    c = os.path.normcase(os.path.abspath(os.fspath(child)))
    p = os.path.normcase(os.path.abspath(os.fspath(parent)))
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def _temp_roots(env: Optional[Dict[str, str]] = None) -> list[Path]:
    """Directories that are "the system temp dir" on the machines we write on."""
    env = os.environ if env is None else env
    roots: list[Path] = []
    for name in ("TEMP", "TMP", "TMPDIR"):
        v = env.get(name)
        if v:
            roots.append(Path(os.path.abspath(v)))
    home = os.path.expanduser("~")
    if home and home != "~":  # no resolvable home: the env vars above still cover us
        roots.append(Path(os.path.abspath(home)) / "AppData" / "Local" / "Temp")
    roots.append(Path("/tmp"))
    roots.append(Path("/var/tmp"))
    return roots


def _is_temp(target: str, env: Optional[Dict[str, str]] = None) -> bool:
    return any(_under(target, r) for r in _temp_roots(env))


def _is_scratch(target: str) -> bool:
    """``.scratch`` anywhere on the path is the session's own workbench."""
    parts = Path(os.path.abspath(target)).parts
    return any(part == ".scratch" for part in parts)


def _is_script(target: str) -> bool:
    """True for automation a job is run from. Data/artifacts are not scripts."""
    return os.path.splitext(target)[1].lower() in SCRIPT_SUFFIXES


def _loose_script_reason(payload: Dict[str, Any], target: str,
                         env: Dict[str, str]) -> Optional[str]:
    """Refuse a loose script that no git work tree owns.

    A work tree is authoritative: anything inside one is reviewable and
    committable, so it passes even when the checkout happens to sit under a
    temp dir (which is where every test repo lives) or the file is untracked
    scratch. Outside every work tree there is nothing to review it, so the
    script is refused whether or not a stale copy already sits there -- a
    yesterday's loose script in temp is the exact case that must stop. The
    temp-dir phrasing is kept for the reason text because it is the owner's
    own example."""
    if env.get(LOOSE_OVERRIDE_ENV, "").strip() in ("1", "true", "yes"):
        return None
    if _is_scratch(target) or not _is_script(target):
        return None
    if worktree_of(target) is not None:
        return None
    in_temp = _is_temp(target, env)
    owner = worktree_of(payload.get("cwd") or os.getcwd())
    where = ("under the system temp dir" if in_temp
             else "outside any git work tree")
    owning = (f"an existing work tree ({owner})" if owner
              else "the repo that should own this change")
    return (
        f"{target} is a loose script {where} with no git work tree to own it: "
        "hand-rolled automation that nothing reviews or can reproduce is exactly "
        "what must never be written again. Do not author loose scripts or start "
        "bare processes. Make the change in a real repo instead: "
        f"`awgit wt new <name>` in {owning} "
        "(then commit + PR to develop; it goes live on merge). For the job itself "
        "use a platform lane: awrun, awsh_awrun_queue, mediaforge_run_op "
        "(film_assemble / mux_audio / film_render), or mcp__aitheros__*. If that "
        "lane does not exist or is broken, building/repairing it in its own repo "
        "is priority 1: file it on Atlas (tasks_create priority=1) and fix it "
        f"before running this job. A deliberate one-off may set {LOOSE_OVERRIDE_ENV}=1."
    )


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
    if tree is not None and not _allowed(tree, target):
        return (
            f"{tree} is a deploy tree (awgit.deploytree=true): containers serve it and the sync "
            "lane moves it to the branch tip after every merge, so edits made here are never "
            "committed and block the next deploy. Make this change in a worktree instead: "
            "`awgit wt new <name>` (then commit + PR to develop; it goes live on merge). "
            f"Only a deliberate hand fix may set {OVERRIDE_ENV}=1."
        )
    return _loose_script_reason(payload, target, env)


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
    _wt_cache.clear()
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
