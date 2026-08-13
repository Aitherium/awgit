"""A stable id for a CHANGE, as opposed to a commit.

A git sha identifies a snapshot. Amend it, rebase it, or cherry-pick it and the
sha is different while the change is the same — which is exactly the question
``push`` has to answer: *is there already a pull request for this?* Matching on
sha says no every time you revise, and opens a second PR for the same work.

So the identity is carried IN the commit, as a trailer::

    Awgit-Change-Id: I3f2a...   (40 hex, Gerrit-style ``I`` prefix)

Written once by a ``prepare-commit-msg`` fragment and never rewritten. git
preserves the message across ``--amend``, ``rebase``, ``cherry-pick`` and
``filter-branch``, so the trailer survives all of them for free — that is the
whole reason to put it there rather than in a side table keyed on sha, which
would have to be migrated on every one of those operations and would be wrong
the moment someone rebased outside awgit.

It also travels: a peer who receives an op bundle, or anyone who clones the
repo, can resolve the same change without access to our op-log.

**Deliberately not derived from content.** Two commits with identical trees and
messages are still two different changes (a cherry-pick to another branch is
the standard case), and a content hash would collapse them into one PR.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import List, Optional

TRAILER = "Awgit-Change-Id"

#: ``I`` + 40 hex. The prefix is Gerrit's convention and it earns its place: it
#: makes a Change-Id impossible to mistake for a git sha at a glance, in logs,
#: in a PR body, and in a grep.
_VALUE = re.compile(r"^I[0-9a-f]{40}$")
_LINE = re.compile(rf"^{TRAILER}:\s*(I[0-9a-f]{{40}})\s*$", re.M | re.I)

#: A trailer block is `Key: value` lines. Used to decide whether the new
#: trailer joins the existing block or starts one.
_TRAILER_LINE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:\s")

#: prepare-commit-msg sources where a Change-Id must NOT be minted.
#: `merge` — a merge commit is not one reviewable change and maps to no PR.
#: `squash` — the message carries SEVERAL commits' messages, so it may already
#: hold several Change-Ids; adding one more would make the result ambiguous.
SKIP_SOURCES = frozenset({"merge", "squash"})


def mint() -> str:
    """A fresh Change-Id. Random, not content-derived — see the module docstring."""
    seed = uuid.uuid4().bytes + os.urandom(16) + str(time.time_ns()).encode()
    return "I" + hashlib.sha1(seed).hexdigest()  # noqa: S324 - an id, not a MAC


def is_valid(value: str) -> bool:
    return bool(_VALUE.match(value or ""))


def extract(message: str) -> Optional[str]:
    """The Change-Id in a commit message, or None.

    Returns the LAST one. A squashed or hand-edited message can carry several;
    the last is the one a trailer block would normally treat as authoritative,
    and picking one deterministically beats returning an arbitrary match.
    """
    found = _LINE.findall(message or "")
    return found[-1] if found else None


def _split_body(text: str) -> tuple:
    """(body lines, comment/scissors tail) — git strips the tail before committing.

    The trailer has to land in the BODY. Appending it after git's
    ``# Please enter the commit message`` block, or after a ``>8`` scissors
    line, means git discards it and the change silently has no id.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("#"):
            return lines[:i], lines[i:]
    return lines, []


def add_to_message(text: str) -> str:
    """Return ``text`` with a Change-Id trailer appended (no-op if it has one)."""
    if extract(text):
        return text
    body, tail = _split_body(text)
    while body and not body[-1].strip():
        body.pop()
    if not body:
        # An empty message means the commit is going to be aborted anyway;
        # stamping it would put a trailer in a message nobody wrote.
        return text
    # Join an existing trailer block (Co-Authored-By: ...) rather than starting
    # a new paragraph, so `git interpret-trailers` still sees one block.
    if not _TRAILER_LINE.match(body[-1]):
        body.append("")
    body.append(f"{TRAILER}: {mint()}")
    return "\n".join(body + ([""] if tail else []) + tail)


def ensure_in_file(path: Path, source: str = "") -> Optional[str]:
    """Stamp the message file in place. Returns the id, or None if skipped."""
    if source in SKIP_SOURCES:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    updated = add_to_message(text)
    if updated == text:
        return extract(text)
    path.write_text(updated, encoding="utf-8", newline="\n")
    return extract(updated)


def _git(repo: Optional[Path], *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo) if repo else None, capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout


def of_commit(rev: str = "HEAD", repo: Optional[Path] = None) -> Optional[str]:
    """The Change-Id of one commit, read from its message."""
    try:
        return extract(_git(repo, "log", "-1", "--format=%B", rev))
    except subprocess.CalledProcessError:
        return None


def find(change_id: str, repo: Optional[Path] = None,
         rev_range: str = "HEAD~200..HEAD") -> List[str]:
    """Every commit in ``rev_range`` carrying ``change_id``, newest first.

    More than one is normal, not an error: an amend leaves the old commit
    reachable from the reflog, and a cherry-pick deliberately puts the same
    change on two branches. Callers pick by branch; this only reports.
    """
    if not is_valid(change_id):
        return []
    try:
        out = _git(repo, "log", "--format=%H", f"--grep={TRAILER}: {change_id}",
                   "-i", rev_range)
    except subprocess.CalledProcessError:
        # An unborn branch or a range shorter than the window is not an error.
        try:
            out = _git(repo, "log", "--format=%H", f"--grep={TRAILER}: {change_id}", "-i")
        except subprocess.CalledProcessError:
            return []
    return [line.strip() for line in out.splitlines() if line.strip()]
