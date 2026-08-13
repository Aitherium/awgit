"""``push`` IS the pull request.

There is no ``awgit pr create``. You push, and each commit in the stack becomes
one pull request, diffed against the commit below it — so a reviewer reads one
logical change instead of a 900-line branch, and the bottom of a stack can land
while the top is still being argued about.

Pushing again after ``commit --amend`` adds a REVISION to the same pull request
rather than opening a second one. That works because identity comes from the
``Awgit-Change-Id`` trailer, which survives the amend (see awgit/changeid.py);
matching on sha would open a new PR every time anyone responded to review.

**The mapping is derived, not stored.** Each commit is pushed to a synthetic ref
whose name CONTAINS its Change-Id::

    awgit/<actor>/<changeid-12>

so "which PR is this change?" is answered by asking GitHub which PR has that
head ref. A local id->PR file would be a second source of truth that goes stale
the moment someone pushes from another machine, closes a PR in the web UI, or
clones fresh — and its staleness would be invisible until it opened a duplicate.

**Force-pushing is confined to the ``awgit/`` namespace.** These refs are
written only by this command and are never checked out by a human, so replacing
them is safe; a real branch is not, and :func:`is_synthetic` is asserted before
any force. That check is cheap and the mistake it prevents is not.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: Namespace owned by awgit. Nothing outside it is ever force-pushed.
NAMESPACE = "awgit"

#: Deliberately excludes the DOT. git rejects any refname containing "..", and
#: an actor is caller-supplied: `claude:8f21/../../etc` slugged to
#: `claude-8f21-..-..-etc`, which git refuses with an "invalid refname" error
#: naming a ref the user never typed. Dots buy nothing in an actor slug.
_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")

#: What a finished ref component must look like, asserted before any push.
_COMPONENT = re.compile(r"^[a-zA-Z0-9_-]+$")


class PushError(Exception):
    """A push could not proceed. Always carries what to do about it."""


def slug(actor: str) -> str:
    """A ref-safe actor. `claude:8f21…` -> `claude-8f21…`."""
    cleaned = _SAFE.sub("-", actor).strip("-")
    return cleaned[:40] or "anon"


def synthetic_ref(actor: str, change_id: str) -> str:
    """The branch a commit is published on."""
    if not change_id.startswith("I") or len(change_id) < 13:
        raise PushError(f"not a Change-Id: {change_id!r}")
    return f"{NAMESPACE}/{slug(actor)}/{change_id[1:13]}"


def is_synthetic(ref: str) -> bool:
    """Only refs awgit owns may be force-replaced.

    Checks the SHAPE of every component, not just the prefix: this is the guard
    standing between a force-push and somebody's branch, and a prefix test alone
    would accept `awgit/x/..` — which is not even a legal refname.
    """
    parts = ref.split("/")
    if len(parts) != 3 or parts[0] != NAMESPACE:
        return False
    return all(_COMPONENT.match(part) for part in parts[1:])


@dataclass
class PushStep:
    """What will happen to one commit in the stack."""

    index: int
    sha: str
    change_id: str
    subject: str
    ref: str
    base: str
    existing_pr: Optional[int] = None

    @property
    def action(self) -> str:
        return "update" if self.existing_pr else "create"

    def to_dict(self) -> dict:
        return {
            "index": self.index, "sha": self.sha[:12], "change_id": self.change_id,
            "subject": self.subject, "ref": self.ref, "base": self.base,
            "pr": self.existing_pr, "action": self.action,
        }


@dataclass
class PushPlan:
    steps: List[PushStep] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    trunk: str = ""

    def to_dict(self) -> dict:
        return {"trunk": self.trunk, "steps": [s.to_dict() for s in self.steps],
                "problems": self.problems}


def _git(repo: Optional[Path], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo) if repo else None, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )


def _gh(repo: Optional[Path], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args], cwd=str(repo) if repo else None, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )


def gh_available(repo: Optional[Path] = None) -> Tuple[bool, str]:
    """(usable, why-not). Absence is reported, never guessed around."""
    try:
        proc = _gh(repo, "auth", "status")
    except FileNotFoundError:
        return False, "gh is not on PATH — install the GitHub CLI"
    if proc.returncode != 0:
        return False, "gh is not authenticated — run `gh auth login`"
    return True, ""


def open_prs(repo: Optional[Path] = None) -> Dict[str, int]:
    """head-ref -> PR number, for every open PR in this repo.

    One query, not one per commit: a ten-commit stack would otherwise make ten
    API round-trips before doing any work.
    """
    proc = _gh(repo, "pr", "list", "--state", "open", "--limit", "200",
               "--json", "number,headRefName")
    if proc.returncode != 0:
        return {}
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    return {row["headRefName"]: row["number"] for row in rows
            if row.get("headRefName")}


def plan(actor: str, repo: Optional[Path] = None, trunk: str = "",
         query_github: bool = True) -> PushPlan:
    """What ``push`` would do, bottom-up, one PR per commit."""
    from awgit import stack as stackmod

    resolved = stackmod.detect_trunk(repo, trunk)
    result = PushPlan(trunk=resolved or "")
    if not resolved:
        result.problems.append(
            "no trunk found — pass --trunk, or fetch so origin/HEAD exists")
        return result

    entries = stackmod.load(repo, resolved)
    if not entries:
        result.problems.append(f"no commits above {resolved} — nothing to push")
        return result

    existing = open_prs(repo) if query_github else {}
    # The bottom of the stack is based on TRUNK; every other commit is based on
    # the ref below it. That is what makes each PR show one commit's diff
    # instead of everything since trunk.
    base = resolved.split("/", 1)[-1] if "/" in resolved else resolved
    for entry in entries:
        if not entry.change_id:
            result.problems.append(
                f"{entry.sha[:12]} has no Awgit-Change-Id — `awgit hooks install` "
                f"then rebase to stamp it; push cannot match it to a PR")
            return result
        ref = synthetic_ref(actor, entry.change_id)
        result.steps.append(PushStep(
            index=entry.index, sha=entry.sha, change_id=entry.change_id,
            subject=entry.subject, ref=ref, base=base,
            existing_pr=existing.get(ref),
        ))
        base = ref
    return result


def body_for(step: PushStep, plan_: PushPlan) -> str:
    """The PR body: what this commit is, and where it sits in the stack."""
    lines = [f"_Stacked change {step.index + 1} of {len(plan_.steps)}._", ""]
    lines.append("| | change | PR |")
    lines.append("|---|---|---|")
    for other in reversed(plan_.steps):
        here = "**->**" if other.index == step.index else ""
        pr = f"#{other.existing_pr}" if other.existing_pr else "(new)"
        lines.append(f"| {here} | {other.subject[:60]} | {pr} |")
    lines += ["", f"`{step.change_id}`", "",
              "<sub>Opened by `awgit push`. Amend and push again to add a "
              "revision — do not open a second PR.</sub>"]
    return "\n".join(lines)


def apply(plan_: PushPlan, repo: Optional[Path] = None) -> Tuple[bool, List[str]]:
    """Publish the stack: one ref and one PR per commit, bottom-up."""
    messages: List[str] = []
    if plan_.problems:
        return False, list(plan_.problems)
    ok, why = gh_available(repo)
    if not ok:
        return False, [why]

    for step in plan_.steps:
        if not is_synthetic(step.ref):
            # Unreachable via synthetic_ref(); asserted anyway because the
            # consequence of being wrong is a force-push over a real branch.
            return False, [f"refusing to force-push {step.ref!r}: not in the "
                           f"{NAMESPACE}/ namespace"]
        pushed = _git(repo, "push", "--force", "origin",
                      f"{step.sha}:refs/heads/{step.ref}")
        if pushed.returncode != 0:
            return False, messages + [
                f"push of {step.ref} failed: {(pushed.stderr or '').strip()}"]

        title = step.subject
        body = body_for(step, plan_)
        if step.existing_pr:
            edited = _gh(repo, "pr", "edit", str(step.existing_pr),
                         "--base", step.base, "--title", title, "--body", body)
            if edited.returncode != 0:
                return False, messages + [
                    f"pr edit #{step.existing_pr} failed: "
                    f"{(edited.stderr or '').strip()}"]
            messages.append(f"updated #{step.existing_pr}  {title[:56]}")
        else:
            created = _gh(repo, "pr", "create", "--head", step.ref,
                          "--base", step.base, "--title", title, "--body", body)
            if created.returncode != 0:
                return False, messages + [
                    f"pr create for {step.ref} failed: "
                    f"{(created.stderr or '').strip()}"]
            messages.append(f"opened   {created.stdout.strip()[:70]}")
    return True, messages


def render(plan_: PushPlan) -> List[str]:
    lines: List[str] = []
    for problem in plan_.problems:
        lines.append(f"  !! {problem}")
    for step in reversed(plan_.steps):
        marker = f"#{step.existing_pr}" if step.existing_pr else "new"
        lines.append(f"  {step.action:<6} {marker:>6}  {step.subject[:52]}")
        lines.append(f"         {step.ref}  ->  {step.base}")
    if plan_.steps:
        lines.append(f"  ({len(plan_.steps)} commit(s), one PR each, "
                     f"bottom based on {plan_.trunk})")
    return lines
