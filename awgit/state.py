"""One snapshot of where you are: branch, worktree, stack, PRs, merge state.

WHY THIS EXISTS
---------------------------------------------------------------------------
Every agent touching a shared tree needs the same five facts before it edits
anything -- which branch, which worktree, what is stacked, what is already open
as a PR, and whether a merge is in flight -- and until now each one re-derived
them with its own `git` calls, or did not ask at all. The primitives were all
here already (``worktree``, ``stack``, ``push``, ``merge``, ``sync``); what was
missing was a single call composing them, and consumers reaching for it.

So this module COMPOSES. It reimplements nothing. If an answer looks wrong, the
bug is in the module that owns that question, and there is exactly one of those.

THE PART THAT IS NOT OPTIONAL: every field degrades ALONE, and says why
---------------------------------------------------------------------------
A snapshot is read by something deciding whether it is safe to write. The
dangerous failure is not an exception -- it is a field that comes back empty
because the tool behind it was missing, and reads as "nothing here" instead of
"I could not look". ``open_prs`` with no ``gh`` on PATH returns ``{}``; so does a
repo with genuinely no open PRs. Those must never be the same answer, because
one of them means "nothing to collide with" and the other means "I have no idea
what you might collide with".

So every field is either a real value or ``None``, and every ``None`` has an
entry in ``unavailable`` naming the reason. A caller that ignores ``unavailable``
gets the same behaviour it would have had; a caller that reads it can tell the
two apart. Nothing here raises: one broken input degrades one field.

This ships to PyPI inside awgit, so there are no monorepo imports and no host
paths -- a stranger installs it and it works on their repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import merge, push, stack, worktree

__all__ = ["snapshot", "render", "self_test"]

_TIMEOUT = 20


def _git(repo: Optional[Path], *args: str) -> Optional[str]:
    """stdout of a git command, or None if it could not run or failed.

    None means "no answer", never "empty answer" -- the caller decides which of
    those it can live with.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo) if repo else None,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=_TIMEOUT, check=False)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _ahead_behind(repo: Optional[Path], left: str, right: str):
    """(ahead, behind) of left vs right, or None if either ref does not resolve."""
    out = _git(repo, "rev-list", "--left-right", "--count", f"{left}...{right}")
    if not out:
        return None
    parts = out.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1])


def snapshot(repo: Optional[Path] = None) -> Dict[str, Any]:
    """Where you are, as data. Never raises.

    Keys whose value is None are listed in ``unavailable`` with the reason, so
    "I could not look" is always distinguishable from "there is nothing here".
    """
    repo_path = Path(repo) if repo else None
    snap: Dict[str, Any] = {}
    why: Dict[str, str] = {}

    # --- is this a repo at all -------------------------------------------------
    top = _git(repo_path, "rev-parse", "--show-toplevel")
    snap["repo"] = top
    if top is None:
        why["repo"] = "not a git repository (or git is not on PATH)"
        # Every other question is meaningless without this one, and answering
        # them with None-and-no-reason is exactly the silence this module exists
        # to remove. Say it once, per field, and stop.
        for key in ("branch", "head", "dirty", "trunk", "stack", "open_prs",
                    "worktrees", "conflicts", "upstream", "ahead_behind",
                    "trunk_ahead_behind", "detached"):
            snap[key] = None
            why[key] = "no repository"
        snap["unavailable"] = why
        return snap

    # --- branch / HEAD ---------------------------------------------------------
    branch = _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    snap["detached"] = (branch == "HEAD") if branch is not None else None
    snap["branch"] = None if branch in (None, "HEAD") else branch
    if branch is None:
        why["branch"] = "git could not read HEAD"
    elif branch == "HEAD":
        why["branch"] = "detached HEAD -- no branch to name"

    snap["head"] = _git(repo_path, "rev-parse", "HEAD")
    if snap["head"] is None:
        why["head"] = "no commits yet, or git could not read HEAD"

    status = _git(repo_path, "status", "--porcelain")
    snap["dirty"] = None if status is None else bool(status)
    snap["dirty_count"] = None if status is None else len(
        [ln for ln in status.splitlines() if ln.strip()])
    if status is None:
        why["dirty"] = "git status failed"

    # --- upstream + divergence -------------------------------------------------
    upstream = _git(repo_path, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                    "@{upstream}")
    snap["upstream"] = upstream
    if upstream is None:
        why["upstream"] = "this branch tracks no remote"
        snap["ahead_behind"] = None
        why["ahead_behind"] = "no upstream to compare against"
    else:
        ab = _ahead_behind(repo_path, "HEAD", upstream)
        snap["ahead_behind"] = None if ab is None else {"ahead": ab[0], "behind": ab[1]}
        if ab is None:
            why["ahead_behind"] = "upstream ref did not resolve (fetch it?)"

    # --- trunk + stack ---------------------------------------------------------
    try:
        trunk = stack.detect_trunk(repo_path)
    except Exception as exc:
        trunk = None
        why["trunk"] = f"detect_trunk failed: {type(exc).__name__}"
    snap["trunk"] = trunk
    if trunk is None and "trunk" not in why:
        why["trunk"] = "no trunk branch found (no main/develop/master)"

    if trunk:
        ab = _ahead_behind(repo_path, "HEAD", trunk)
        snap["trunk_ahead_behind"] = None if ab is None else {
            "ahead": ab[0], "behind": ab[1]}
        if ab is None:
            why["trunk_ahead_behind"] = f"{trunk} did not resolve"
    else:
        snap["trunk_ahead_behind"] = None
        why["trunk_ahead_behind"] = "no trunk to compare against"

    try:
        entries = stack.load(repo_path, trunk or "")
        snap["stack"] = [e.to_dict() for e in entries]
    except Exception as exc:
        snap["stack"] = None
        why["stack"] = f"stack.load failed: {type(exc).__name__}"

    # --- open PRs --------------------------------------------------------------
    # The distinction this whole module is about: gh absent must NOT look like
    # "no open PRs".
    try:
        ok, detail = push.gh_available(repo_path)
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}"
    if not ok:
        snap["open_prs"] = None
        why["open_prs"] = f"gh unavailable: {detail or 'not on PATH'}"
    else:
        try:
            snap["open_prs"] = dict(push.open_prs(repo_path))
        except Exception as exc:
            snap["open_prs"] = None
            why["open_prs"] = f"gh pr list failed: {type(exc).__name__}"

    # --- worktrees -------------------------------------------------------------
    try:
        snap["worktrees"] = [
            {"path": p, "sha": s, "branch": b} for p, s, b in worktree.listing(repo_path)
        ]
    except Exception as exc:
        snap["worktrees"] = None
        why["worktrees"] = f"worktree listing failed: {type(exc).__name__}"

    # --- merge in flight -------------------------------------------------------
    git_dir = _git(repo_path, "rev-parse", "--git-dir")
    if git_dir is None:
        snap["merging"] = None
        why["merging"] = "could not locate the git dir"
    else:
        base = Path(top) / git_dir if not Path(git_dir).is_absolute() else Path(git_dir)
        snap["merging"] = any(
            (base / n).exists()
            for n in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge",
                      "rebase-apply"))

    try:
        snap["conflicts"] = [c.path for c in merge.list_conflicts()]
    except Exception as exc:
        snap["conflicts"] = None
        why["conflicts"] = f"conflict ledger unreadable: {type(exc).__name__}"

    snap["unavailable"] = why
    return snap


def render(snap: Dict[str, Any]) -> List[str]:
    """Human-readable lines. What could not be determined is SHOWN, not omitted."""
    out: List[str] = []
    if snap.get("repo") is None:
        return ["not a git repository"]

    head = (snap.get("head") or "")[:12] or "?"
    branch = snap.get("branch") or ("(detached)" if snap.get("detached") else "?")
    line = f"branch {branch} @ {head}"
    ab = snap.get("ahead_behind")
    if ab:
        line += f"  [+{ab['ahead']}/-{ab['behind']} vs {snap.get('upstream')}]"
    out.append(line)

    tab = snap.get("trunk_ahead_behind")
    if snap.get("trunk") and tab:
        out.append(f"trunk {snap['trunk']}: +{tab['ahead']}/-{tab['behind']}")

    if snap.get("dirty"):
        out.append(f"worktree DIRTY ({snap.get('dirty_count')} path(s))")
    elif snap.get("dirty") is False:
        out.append("worktree clean")

    st = snap.get("stack")
    if st:
        out.append(f"stack: {len(st)} commit(s) since trunk")
    prs = snap.get("open_prs")
    if prs is not None:
        out.append(f"open PRs: {len(prs)}")
    wts = snap.get("worktrees")
    if wts is not None and len(wts) > 1:
        out.append(f"worktrees: {len(wts)}")
    if snap.get("merging"):
        out.append("MERGE/REBASE IN FLIGHT")
    conf = snap.get("conflicts")
    if conf:
        out.append(f"recorded conflicts: {len(conf)}")

    why = snap.get("unavailable") or {}
    for key in sorted(why):
        out.append(f"  ? {key}: {why[key]}")
    return out


def self_test() -> int:
    """Prove the contract that matters: it never raises, and silence is labelled."""
    import tempfile

    failures: List[str] = []

    # 1. a directory that is not a repo must produce a snapshot, not an exception
    with tempfile.TemporaryDirectory() as td:
        try:
            snap = snapshot(Path(td))
        except Exception as exc:  # pragma: no cover - the thing being asserted
            print(f"SELF-TEST FAILED: snapshot raised outside a repo: {exc!r}")
            return 1
        if snap.get("repo") is not None:
            failures.append("a non-repo reported a repo root")
        if not snap.get("unavailable"):
            failures.append(
                "a non-repo produced no `unavailable` reasons -- a caller cannot "
                "tell that from a healthy empty repo, which is the whole point")
        for key in ("branch", "open_prs", "stack"):
            if key not in snap:
                failures.append(f"{key} missing from the snapshot entirely")
            elif snap.get(key) is not None:
                failures.append(f"{key} answered a value outside a repo")
            elif key not in (snap.get("unavailable") or {}):
                failures.append(f"{key} is None with NO reason -- silence as data")
        if render(snap) != ["not a git repository"]:
            failures.append("render did not degrade cleanly outside a repo")

    # 2. every None in a real snapshot carries a reason
    snap = snapshot(None)
    why = snap.get("unavailable") or {}
    for key, val in snap.items():
        if key == "unavailable":
            continue
        if val is None and key not in why:
            failures.append(f"{key} is None with no entry in `unavailable`")
    if not isinstance(render(snap), list):
        failures.append("render did not return a list")

    if failures:
        print("SELF-TEST FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SELF-TEST PASSED - snapshot never raises, and every None names its "
          "reason so 'could not look' is distinguishable from 'nothing here'")
    return 0


# ---------------------------------------------------------------------------
# The consumer entry point.
#
# Every aw* CLI needs the same three lines, so the RULE lives here once rather
# than being re-decided per package. Two copies of one rule drift -- that is not
# a risk, it is what happened to the shared browser-inference worker while a
# comment inside it asked people to keep them in step.
#
# Two surfaces on purpose:
#   --repo-state   an explicit ask; prints and the caller exits
#   AW_REPO_STATE  an env opt-in, so an agent can have it on EVERY invocation
#                  without every call site growing a flag
#
# It goes to STDERR and never to stdout: these CLIs are piped, and a banner in
# stdout would corrupt whatever reads them. Unset is OFF -- a multi-hundred-line
# banner nobody asked for is the same defect as an unasked download.
# ---------------------------------------------------------------------------

def cli_banner(argv=None, stream=None) -> bool:
    """Handle --repo-state / AW_REPO_STATE. True if the caller should exit now.

    Never raises and never blocks the command it decorates: a CLI that cannot
    describe its repo must still do its job.
    """
    import os
    import sys as _sys

    argv = list(argv or [])
    out = stream if stream is not None else _sys.stderr
    explicit = "--repo-state" in argv
    env_on = os.environ.get("AW_REPO_STATE", "").strip().lower() not in ("", "0", "false", "no")
    if not explicit and not env_on:
        return False
    try:
        for line in render(snapshot()):
            print(f"[repo] {line}", file=out)
    except Exception as exc:  # pragma: no cover - the contract is "never block"
        print(f"[repo] unavailable: {type(exc).__name__}", file=out)
    return explicit
