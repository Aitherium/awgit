"""The stack: your commits since trunk, each one a reviewable change.

There are no local branches in this model. What you are working on is the
sequence of commits between trunk and HEAD, and each becomes its own pull
request — so a reviewer sees one logical change at a time instead of a 900-line
branch, and a change can land the moment IT is ready rather than waiting for
everything stacked above it.

Position is derived from git, never stored. A stack is
``merge-base(trunk, HEAD)..HEAD`` in commit order; there is no state file to go
stale, and a rebase or an amend simply produces a different answer to the same
question. Identity comes from the Change-Id trailer, so a commit keeps its
place — and its pull request — across every rewrite (see awgit/changeid.py).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

#: Trunk candidates, in order. A repo names its trunk one of these, and
#: guessing wrong turns the whole stack into "every commit ever made".
TRUNK_CANDIDATES = ("origin/HEAD", "origin/main", "origin/develop", "origin/master",
                    "main", "develop", "master")


@dataclass
class StackEntry:
    """One commit in the stack. ``index`` 0 is the bottom (nearest trunk)."""

    index: int
    sha: str
    change_id: str
    subject: str
    is_head: bool

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "sha": self.sha,
            "short_sha": self.sha[:12],
            "change_id": self.change_id,
            "subject": self.subject,
            "is_head": self.is_head,
        }


def _git(repo: Optional[Path], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo) if repo else None, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )


def _ok(repo: Optional[Path], *args: str) -> str:
    proc = _git(repo, *args)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def detect_trunk(repo: Optional[Path] = None, explicit: str = "") -> Optional[str]:
    """The trunk this stack is measured against.

    ``origin/HEAD`` first because it is what the REMOTE says its default branch
    is, rather than what this checkout happens to have. Falling straight to a
    hardcoded "main" gets the wrong answer on every repo whose trunk is
    ``develop`` — and the wrong answer here is not an error, it is a stack
    containing the entire history, which reads as a working command.
    """
    if explicit:
        return explicit if _ok(repo, "rev-parse", "--verify", f"{explicit}^{{commit}}") else None
    for candidate in TRUNK_CANDIDATES:
        if _ok(repo, "rev-parse", "--verify", f"{candidate}^{{commit}}"):
            return candidate
    return None


def load(repo: Optional[Path] = None, trunk: str = "") -> List[StackEntry]:
    """The stack, bottom-first. Empty when HEAD is on trunk or trunk is unknown."""
    from awgit.changeid import extract

    resolved = detect_trunk(repo, trunk)
    if not resolved:
        return []
    base = _ok(repo, "merge-base", resolved, "HEAD")
    if not base:
        return []
    head = _ok(repo, "rev-parse", "HEAD")
    # %x00 separates the subject from the body so a multi-line message cannot
    # be mistaken for another commit's record.
    out = _ok(repo, "log", "--reverse", "--format=%H%x00%s%x00%b%x1e", f"{base}..HEAD")
    entries: List[StackEntry] = []
    for i, record in enumerate(r for r in out.split("\x1e") if r.strip()):
        parts = record.strip().split("\x00")
        if len(parts) < 2:
            continue
        sha, subject = parts[0].strip(), parts[1]
        body = parts[2] if len(parts) > 2 else ""
        entries.append(StackEntry(
            index=i, sha=sha, change_id=extract(body) or "",
            subject=subject, is_head=(sha == head),
        ))
    return entries


def position(entries: List[StackEntry]) -> Optional[int]:
    """Index of HEAD within the stack, or None when HEAD is not in it."""
    for entry in entries:
        if entry.is_head:
            return entry.index
    return None


def neighbour(entries: List[StackEntry], step: int) -> Optional[StackEntry]:
    """The entry ``step`` places from HEAD (-1 = down/older, +1 = up/newer)."""
    here = position(entries)
    if here is None:
        return None
    target = here + step
    if 0 <= target < len(entries):
        return entries[target]
    return None


def orphans(repo: Optional[Path] = None, trunk: str = "") -> List[StackEntry]:
    """Commits ORPHANED by a rewrite — in the reflog, not in the stack any more.

    Amend a commit in the middle of a stack and everything above it is left
    pointing at the commit you replaced. git does not follow; those commits are
    still in the reflog and reachable from nothing. This is the single most
    common stacked-diff operation, and without repair the amend silently DROPS
    the rest of your stack — measured doing exactly that during an end-to-end
    push test: `prev`, amend, `restack`, and the third commit was gone with
    "HEAD is up to date" printed.

    Identity is what makes the repair possible: an orphan carries the same
    Change-Id it always had, so "which commits used to be in this stack and no
    longer are" is answerable without any stored state.
    """
    from awgit.changeid import extract

    here = {e.change_id for e in load(repo, trunk) if e.change_id}
    head = _ok(repo, "rev-parse", "HEAD")
    seen: List[StackEntry] = []
    known: set = set()
    out = _ok(repo, "reflog", "--format=%H", "-n", "80")
    for i, sha in enumerate(line.strip() for line in out.splitlines() if line.strip()):
        if sha in known:
            continue
        known.add(sha)
        # Reachable from HEAD means it is still part of the stack (or history).
        if _git(repo, "merge-base", "--is-ancestor", sha, head).returncode == 0:
            continue
        body = _ok(repo, "log", "-1", "--format=%b", sha)
        cid = extract(body) or ""
        if not cid or cid in here:
            continue
        # Its parent must have been REWRITTEN: same Change-Id still in the
        # stack, but at a DIFFERENT sha. That is what being orphaned means, and
        # nothing weaker works.
        #
        # Two wrong versions preceded this, and the second is the instructive
        # one. "Unreachable in the reflog" replays anything anyone ever threw
        # away. "Child of a commit still in the stack" looks right and is not:
        # a commit discarded with `reset --hard` also has a parent that is still
        # in the stack, unchanged — so abandoned work sailed through. Only
        # "parent replaced" separates them. Resurrecting a discarded commit is
        # far worse than leaving an orphan: the orphan is visible, and the
        # resurrected commit looks like it was meant to be there.
        parent_sha = _ok(repo, "rev-parse", f"{sha}^")
        parent_cid = extract(_ok(repo, "log", "-1", "--format=%b", f"{sha}^")) or ""
        if parent_cid not in here:
            continue
        current = next((e for e in load(repo, trunk)
                        if e.change_id == parent_cid), None)
        if current is None or current.sha == parent_sha:
            continue  # the parent is unchanged — this was discarded, not orphaned
        here.add(cid)  # one entry per change, newest reflog hit wins
        seen.append(StackEntry(
            index=i, sha=sha, change_id=cid,
            subject=_ok(repo, "log", "-1", "--format=%s", sha), is_head=False))
    # Oldest first, so replaying preserves the original order.
    return list(reversed(seen))


def render(entries: List[StackEntry], trunk: str) -> List[str]:
    """The smartlog: newest at the TOP, the way a stack is drawn."""
    if not entries:
        return [f"awgit: no commits above {trunk or 'trunk'} — the stack is empty"]
    lines: List[str] = []
    for entry in reversed(entries):
        marker = "@" if entry.is_head else "o"
        cid = entry.change_id[:9] if entry.change_id else "-"
        lines.append(f"  {marker} {entry.index}  {entry.sha[:12]}  {cid:<9}  "
                     f"{entry.subject[:60]}")
    lines.append(f"  ~    {trunk}")
    return lines
