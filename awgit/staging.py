"""Stage only YOUR edits to a file other sessions are editing too — and prove it.

THE PROBLEM
-----------
`git add <file>` stages the whole file. In a repo where several agent sessions
write concurrently (this one commits every 2-5 minutes), that silently sweeps
another session's uncommitted work into your commit — a class which has
already destroyed hours of work here once.

The obvious workaround is to filter the diff hunks down to "the ones that look
like mine". That is what was tried on 2026-08-10, and it produced a WORSE failure
than the problem it solved: the filter kept hunks containing the marker
``addon_manifest`` and dropped the hunk that registered the new checker, because
that line reads ``"addon manifest fields match AddonManager"`` — with spaces. The
result was a commit containing a check function that nothing called. Every cheap
signal said done: the file parsed, the tests passed, `git diff --stat` looked
right, and the gate ran nothing.

Marker filtering is unsound because a change is not a set of lines that share a
substring. It is a DIFF, and you cannot recover a diff from a heuristic.

THE FIX
-------
A lease already means "I am about to edit this file", so `awgit lease acquire`
now snapshots the file as a git blob (``Lease.baseline_blob``). Given that:

    mine   = baseline -> working tree
    theirs = baseline -> (everything else that changed)

and the content to stage is HEAD with `mine` replayed on top — a three-way merge
with the baseline as the merge base. `git merge-file` does exactly this, and it
CONFLICTS instead of guessing when your edit and theirs touch the same lines.

Then it verifies: every line `mine` adds must be present in the staged result. If
even one is missing, the stage is refused. That check is what would have caught
the dropped registration, and it is the reason this module exists rather than a
better filter.

THE CONTRACT, AND WHAT IT DOES NOT COVER
----------------------------------------
**Take the lease BEFORE you edit.** The baseline is what separates "everything
that was already here" from "what I did". Acquire it first and another session's
prior work — committed or not — is outside your diff by construction, which is
exactly the real case: their 45 hunks were already in the file when this session
started editing it.

An edit another actor makes AFTER your lease lands inside your diff window and is
**not** separable from yours. That is stated rather than papered over, and it is
narrow by design: path leases are exclusive, so a second actor holding the same
path is refused. It happens only when the other writer took no lease at all — the
case a lease system cannot fix from the inside. `stage_mine` still guarantees the
half that matters most there: your own edits are never silently dropped.
"""

from __future__ import annotations

import difflib
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("awgit.staging")


class StagingError(Exception):
    """A stage that could not be performed safely."""


@dataclass
class StageResult:
    path: str
    staged: bool
    added_lines: int = 0
    removed_lines: int = 0
    foreign_lines_excluded: int = 0
    conflicts: int = 0
    missing: List[str] = field(default_factory=list)
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.staged and not self.missing and not self.conflicts


def _git(args: List[str], repo: Path, *, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120, check=check,
    )


def _blob(sha: str, repo: Path) -> Optional[str]:
    if not sha:
        return None
    proc = _git(["cat-file", "-p", sha], repo)
    return proc.stdout if proc.returncode == 0 else None


def _head_content(rel: str, repo: Path) -> str:
    proc = _git(["show", f"HEAD:{rel}"], repo)
    # A file that is new in this change has no HEAD version; an empty base is
    # correct there, and is distinguishable from "could not read" by returncode.
    return proc.stdout if proc.returncode == 0 else ""


def added_lines(base: str, head: str) -> List[str]:
    """Lines present in ``head`` that are not in ``base``, in order."""
    out: List[str] = []
    for line in difflib.unified_diff(
        base.splitlines(), head.splitlines(), lineterm="", n=0
    ):
        if line.startswith("+") and not line.startswith("+++"):
            out.append(line[1:])
    return out


def removed_lines(base: str, head: str) -> List[str]:
    out: List[str] = []
    for line in difflib.unified_diff(
        base.splitlines(), head.splitlines(), lineterm="", n=0
    ):
        if line.startswith("-") and not line.startswith("---"):
            out.append(line[1:])
    return out


def three_way(head: str, base: str, work: str) -> tuple[str, int]:
    """HEAD with (base -> work) replayed on top. Returns (merged, conflict_count).

    `git merge-file` rather than a hand-rolled splice: it is the same machinery
    git uses for a real merge, it handles interleaved edits, and it marks a true
    conflict instead of silently preferring one side.
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "head").write_text(head, encoding="utf-8", newline="")
        (d / "base").write_text(base, encoding="utf-8", newline="")
        (d / "work").write_text(work, encoding="utf-8", newline="")
        proc = subprocess.run(
            ["git", "merge-file", "-p", "--diff3",
             "-L", "HEAD", "-L", "lease-baseline", "-L", "your-edits",
             str(d / "head"), str(d / "base"), str(d / "work")],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        # merge-file exits with the number of conflicts, or <0 on error.
        if proc.returncode < 0:
            raise StagingError(f"git merge-file failed: {(proc.stderr or '').strip()}")
        return proc.stdout, max(0, proc.returncode)


def stage_mine(
    rel_path: str,
    baseline_blob: str,
    repo: Optional[Path] = None,
    *,
    dry_run: bool = False,
) -> StageResult:
    """Stage HEAD + your edits for ``rel_path``, leaving other sessions' work alone.

    Raises ``StagingError`` when it cannot be done safely — an unrecoverable
    baseline, or a conflict between your edit and someone else's on the same
    lines. Refusing is correct there: the alternative is a commit whose contents
    nobody chose.
    """
    root = repo or Path.cwd()
    target = root / rel_path
    if not target.is_file():
        raise StagingError(f"{rel_path}: not a file in the working tree")

    base = _blob(baseline_blob, root)
    if base is None:
        raise StagingError(
            f"{rel_path}: no lease baseline (blob {baseline_blob or '<none>'} "
            f"unreadable). Take the lease BEFORE editing — without a baseline "
            f"there is no way to tell your edits from another session's."
        )

    work = target.read_text(encoding="utf-8", errors="replace")
    head = _head_content(rel_path, root)

    mine_added = added_lines(base, work)
    mine_removed = removed_lines(base, work)
    if not mine_added and not mine_removed:
        return StageResult(rel_path, staged=False, note="no edits since the lease baseline")

    merged, conflicts = three_way(head, base, work)
    if conflicts:
        raise StagingError(
            f"{rel_path}: {conflicts} conflict(s) between your edits and another "
            f"session's on the same lines. Resolve by hand — staging either side "
            f"here would silently discard the other."
        )

    # THE ASSERTION THIS MODULE EXISTS FOR: every line your edit adds must survive
    # into what gets staged. The marker-filter bug passed every other check and
    # failed exactly this one.
    merged_lines = set(merged.splitlines())
    missing = [ln for ln in mine_added if ln.strip() and ln not in merged_lines]

    foreign = len([ln for ln in added_lines(head, work) if ln not in set(mine_added)])

    result = StageResult(
        rel_path, staged=False,
        added_lines=len(mine_added), removed_lines=len(mine_removed),
        foreign_lines_excluded=foreign, conflicts=conflicts, missing=missing,
    )
    if missing:
        result.note = (
            f"REFUSED: {len(missing)} line(s) you wrote are absent from the merged "
            f"result. Staging this would commit an incomplete change."
        )
        return result
    if dry_run:
        result.note = "dry run — nothing staged"
        return result

    # Write the merged content as a blob and put it in the index directly, so the
    # WORKING TREE is never touched. The other session keeps typing into a file
    # this command does not modify.
    proc = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=root, input=merged, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    if proc.returncode != 0:
        raise StagingError(f"{rel_path}: could not write blob: {(proc.stderr or '').strip()}")
    sha = proc.stdout.strip()

    mode = "100644"
    ls = _git(["ls-files", "-s", "--", rel_path], root)
    if ls.returncode == 0 and ls.stdout.strip():
        mode = ls.stdout.split()[0]

    upd = _git(["update-index", "--add", "--cacheinfo", f"{mode},{sha},{rel_path}"], root)
    if upd.returncode != 0:
        raise StagingError(f"{rel_path}: update-index failed: {(upd.stderr or '').strip()}")

    result.staged = True
    result.note = (
        f"staged {len(mine_added)} added / {len(mine_removed)} removed line(s); "
        f"{foreign} line(s) from other sessions left uncommitted"
    )
    return result


def verify_staged(rel_path: str, must_contain: List[str], repo: Optional[Path] = None) -> List[str]:
    """Return the entries of ``must_contain`` absent from the STAGED copy.

    Deliberately reads the index (``git show :path``) and not the working tree.
    The bug this guards against looked correct in the working tree and was wrong
    in the index, which is the copy that becomes the commit.
    """
    root = repo or Path.cwd()
    proc = _git(["show", f":{rel_path}"], root)
    if proc.returncode != 0:
        return list(must_contain)
    staged = proc.stdout
    return [needle for needle in must_contain if needle not in staged]
