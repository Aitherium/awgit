"""Is it safe to rewrite history here?

``absorb``, ``split``, ``fold``, ``uncommit``, ``restack`` and ``pull`` all
rewrite commits. In a checkout you have to yourself that is routine. In a
worktree several agents share it is how hours of somebody else's work
disappear, and this repo has the incident to prove it.

**The naive guard is "only allow rewrites in a linked worktree", and it is
wrong.** It would refuse every rewrite for a solo developer with one clone —
which is the normal way to use a source-control tool, and garden's entire
audience. A guard that makes the feature unusable for its main user gets
switched off, and then it protects nobody.

So ask the question that actually matters: **is anyone else working in this
tree?** awgit can answer it, because the lease registry knows who is holding
what. A stranger's clone has no other actors, no foreign leases, and rewrites
freely. A shared worktree with three live sessions refuses and says where to
go instead.

Three ways to be safe, in order of how cheaply they are established:

1. **A linked worktree** (``git worktree add``) — isolated by construction, so
   nothing else needs checking. This is the answer the refusal points you at.
2. **Nobody else holds a lease here** — a solo checkout, which is most of them.
3. **Explicitly overridden** — ``AWGIT_ALLOW_UNSAFE_REWRITE=1``, for the case
   the tool has no way to know is fine. It is a variable rather than a flag on
   purpose: a per-command ``--force`` is typed reflexively, and an exported
   variable is a decision someone made once and can be seen in the environment.

The refusal always names the fix. A bare denial is how a guard becomes the
thing people work around instead of the thing that helps.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

OVERRIDE_ENV = "AWGIT_ALLOW_UNSAFE_REWRITE"


@dataclass
class Verdict:
    """Why a rewrite is (not) allowed. ``ok`` is the only thing callers branch on."""

    ok: bool
    basis: str = ""
    reasons: List[str] = field(default_factory=list)
    fix: List[str] = field(default_factory=list)

    def report(self) -> str:
        """The refusal, as a human reads it.

        Only the FIRST line carries the headline; the rest are indented detail.
        Repeating "refusing to rewrite history" on all nine lines turned the
        one sentence that matters into wallpaper, and the fix at the bottom —
        the only actionable part — into more of the same.
        """
        head, *rest = self.reasons or ["unsafe"]
        lines = [f"awgit: refusing to rewrite history — {head}"]
        lines += [f"awgit: {r}" for r in rest]
        lines += [f"awgit: {f}" for f in self.fix]
        return "\n".join(lines)


def _git(repo: Optional[Path], *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo) if repo else None, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    ).stdout


def repo_root(cwd: Optional[Path] = None) -> Optional[Path]:
    out = _git(cwd, "rev-parse", "--show-toplevel").strip()
    return Path(out) if out else None


def is_linked_worktree(repo: Path) -> bool:
    """True in a ``git worktree add`` checkout.

    A linked worktree's ``.git`` is a FILE containing ``gitdir: <path>``; the
    main worktree's is a directory. That is the whole distinction, and it is
    cheaper and more reliable than comparing paths against a configured root.
    """
    dot = repo / ".git"
    return dot.is_file()


def foreign_leases(actor: str, repo: Path) -> List[str]:
    """Active leases held by somebody OTHER than ``actor``, described for a human.

    This is the signal a plain git tool does not have. Anyone else holding a
    lease here means a second writer is live in this tree, and a rebase would
    rewrite commits under them.
    """
    try:
        from awgit.leases import LeaseRegistry
    except ImportError:
        return []
    try:
        active = LeaseRegistry().active_leases()
    except Exception:  # noqa: BLE001 - an unreadable registry must not be a pass
        return ["the lease registry could not be read, so peers cannot be ruled out"]
    return [f"{lease.actor} holds {lease.target}" for lease in active
            if lease.actor != actor]


def check(actor: str, cwd: Optional[Path] = None) -> Verdict:
    """May this process rewrite history in ``cwd``?"""
    if os.environ.get(OVERRIDE_ENV) == "1":
        return Verdict(True, basis=f"{OVERRIDE_ENV}=1")

    repo = repo_root(cwd)
    if repo is None:
        return Verdict(
            False,
            reasons=["this is not a git worktree"],
            fix=["run inside a repository"],
        )

    if is_linked_worktree(repo):
        return Verdict(True, basis=f"linked worktree at {repo}")

    peers = foreign_leases(actor, repo)
    if peers:
        shown = peers[:5]
        more = len(peers) - len(shown)
        return Verdict(
            False,
            reasons=[f"{len(peers)} other actor(s) are live in this worktree"]
            + [f"  {p}" for p in shown]
            + ([f"  … and {more} more"] if more else []),
            fix=[
                "rewriting commits here can rewrite theirs. Work in your own"
                " worktree instead:",
                "  awgit worktree new <name>",
                f"or set {OVERRIDE_ENV}=1 if you know this tree is yours alone.",
            ],
        )

    return Verdict(True, basis="no other actor holds a lease in this worktree")


def require(actor: str, cwd: Optional[Path] = None) -> Optional[int]:
    """``None`` when a rewrite may proceed, else an exit code after reporting.

    Shaped so every rewriting command is one line::

        rc = guard.require(_actor(args))
        if rc is not None:
            return rc

    The verdict is printed HERE rather than returned as a bool, so no caller
    can refuse silently — a rewrite that stops with no explanation is
    indistinguishable from one that did nothing.
    """
    import sys

    verdict = check(actor, cwd)
    if verdict.ok:
        return None
    print(verdict.report(), file=sys.stderr)
    return 1
