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


#: The branch a PUBLISHED artifact is supposed to come from. Overridable, because
#: a product with its own release branch is a real thing -- but it has a default,
#: because "which branch should this have been built from" is not a question the
#: person mid-deploy should have to answer.
DEFAULT_DEPLOY_REF = "origin/develop"


@dataclass
class ShipVerdict:
    """Is this working tree fit to build a PUBLISHED artifact from?

    Separate from `Verdict` on purpose: that one asks whether rewriting history
    here would destroy a peer's work, this one asks whether shipping from here
    would publish something old. Same tree, different danger.
    """

    ok: bool
    behind: int = 0
    ahead: int = 0
    ref: str = DEFAULT_DEPLOY_REF
    head: str = ""
    reasons: List[str] = field(default_factory=list)
    fix: List[str] = field(default_factory=list)
    #: True when the question could not be ANSWERED (no such ref, not a repo).
    #: Callers must treat this as a refusal, never as a pass -- an unanswerable
    #: staleness check is exactly the state that let a stale build ship.
    unknown: bool = False

    def report(self) -> str:
        head, *rest = self.reasons or ["unfit to publish from"]
        lines = [f"awgit: refusing to publish — {head}"]
        lines += [f"awgit: {r}" for r in rest]
        lines += [f"awgit: {f}" for f in self.fix]
        return "\n".join(lines)


def behind_deploy_ref(
    cwd: Optional[Path] = None, ref: str = DEFAULT_DEPLOY_REF
) -> ShipVerdict:
    """How far the current checkout is behind the branch releases come from.

    🚨 WHY THIS EXISTS, measured 2026-08-20. Two customer-facing sites were
    published from a feature branch **587 commits behind the deploy branch**. The build succeeded, every gate
    passed, the export was byte-valid, and the sites came up looking right. What
    shipped was a desktop carrying a bug that had been FIXED on develop earlier
    that same day (the Aeon window could not be dragged, because its body was not
    a containing block, so app content painted over the only drag handle).

    Nothing in the pipeline could see it. A build does not know what it is missing;
    `git status` is clean on a stale branch; and the artifact is *correct*, just
    old. The only signal was the owner recognising a bug he had already had fixed.

    Two hours of that session were also spent re-deriving fixes that already
    existed on develop -- the same route exclusion, the same malformed blog post.
    Staleness does not only ship old code, it burns the time spent rebuilding what
    someone already built.

    Answers from the LOCAL ref: this must be usable in a publish path that may have
    no network, and a fetch that silently fails would turn "up to date" into a lie.
    `stale_ref_age_days` reports how old the local ref is so a caller can insist on
    a fetch when it matters, rather than this function pretending to know.
    """
    repo = repo_root(cwd)
    if repo is None:
        return ShipVerdict(
            ok=False, unknown=True, ref=ref,
            reasons=["not inside a git repository, so staleness cannot be judged"],
            fix=["run this from the repo, or pass an explicit --allow-stale reason"],
        )

    head = _git(repo, "rev-parse", "--short", "HEAD").strip()
    # `rev-list --count A..B` counts commits in B not in A.
    raw = _git(repo, "rev-list", "--left-right", "--count", f"HEAD...{ref}").strip()
    parts = raw.split()
    if len(parts) != 2 or not all(x.isdigit() for x in parts):
        return ShipVerdict(
            ok=False, unknown=True, ref=ref, head=head,
            reasons=[f"could not compare HEAD against {ref} (is it fetched?)"],
            fix=["git fetch origin && retry, or pass --allow-stale with a reason"],
        )
    ahead, behind = int(parts[0]), int(parts[1])
    if behind == 0:
        return ShipVerdict(ok=True, behind=0, ahead=ahead, ref=ref, head=head)

    return ShipVerdict(
        ok=False, behind=behind, ahead=ahead, ref=ref, head=head,
        reasons=[
            f"HEAD ({head}) is {behind} commit(s) BEHIND {ref}",
            "a build from here succeeds and ships code that was already fixed —"
            " there is no other signal, because the artifact is correct, just old",
        ],
        fix=[
            f"fix: git fetch origin && git merge {ref}   (or rebuild from {ref})",
            "override only with a stated reason: --allow-stale '<why>'",
        ],
    )


def stale_ref_age_days(cwd: Optional[Path] = None,
                       ref: str = DEFAULT_DEPLOY_REF) -> Optional[float]:
    """Age in days of the LOCAL copy of `ref`, or None if it cannot be read.

    `behind_deploy_ref` compares against whatever was last fetched. A ref fetched
    a week ago can report "0 behind" and be badly wrong, so the age is the second
    half of the answer and callers should surface it.
    """
    repo = repo_root(cwd)
    if repo is None:
        return None
    out = _git(repo, "log", "-1", "--format=%ct", ref).strip()
    if not out.isdigit():
        return None
    import time

    return max(0.0, (time.time() - int(out)) / 86400.0)


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
