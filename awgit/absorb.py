"""Route pending changes back into the commits they belong to.

You review a stack, get three comments across three files, fix them, and now
have a pile of pending edits that each belong to a DIFFERENT commit. Doing that
by hand is a sequence of fixups and an interactive rebase; ``absorb`` does it in
one command.

**awgit routes by NODE, not by line blame, and that is the whole difference.**
git's answer to "which commit owns this change" is per-LINE: it blames the lines
you touched and picks the commit that last wrote them. That is wrong exactly
when it matters — reindent a function and every line blames the reformat; move
a function and every line blames the move; add a line to a body and it blames
whichever neighbouring line happens to sit above it.

awgit asks a different question, because it has stable node ids: *which commit
last changed THIS FUNCTION?* The answer survives reindentation and line movement
outright, and a rename within a file through the redirect capture registers.
This is only possible because the op-log has been recording node-level edits all
along — it is the first place that history pays for itself.

One limit, stated rather than implied: the node id is keyed on (name, path), and
capture's rename detection is scoped to a single file. A function MOVED to
another file therefore gets a new id, and absorb sees a delete plus an add.

Ownership is read from the op-log, and computed directly from the commit when
the op-log has no record (a commit made before the hooks were installed, or by
a client without them). Missing history degrades to a slower answer, never to a
wrong one.

Dry-run by default. It rewrites history, so it goes through ``awgit.guard``
first, and it shows you the routing before it acts.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: How far back to look for a node's owner when the op-log cannot answer.
#:
#: Small on purpose. `absorb` is for "I have a few review fixes on a stack";
#: it is not a history-archaeology tool. The op-log answers ownership as an
#: INDEX and costs nothing, but a commit it has no record of (made before the
#: hooks, or by another client) has to be re-derived: a diff-tree plus a parse
#: of every file it touched.
#:
#: Measured on this repo, which is the pathological case — a 268-commit branch
#: with 383 changed code files — an unbounded walk did not finish in 2.5
#: minutes. Bounded at 30 it answers in seconds, and the commits it did not
#: search are REPORTED, never silently dropped: a node whose owner is out of
#: range shows up as "new work", and absorb would leave it behind while looking
#: like it had considered everything. Raise it with `--depth`.
MAX_OWNER_WALK = 30

#: How many pending files to scan for node changes before stopping.
#:
#: Scanning a file costs a `git show` plus a parse; for the non-Python
#: languages that is tree-sitter, and it is not cheap. Measured on this repo
#: (2477 dirty files, 383 of them parseable), scanning them all did not finish
#: in 110 seconds — the parses, not the ownership search, are the whole cost.
#:
#: `absorb` is for the handful of files you just edited in response to review,
#: so the default is bounded and the truncation is REPORTED. Scope it properly
#: with `--paths`, or lift the bound with `--all`.
MAX_PENDING_FILES = 50


@dataclass
class Routing:
    """One pending node change and the commit it belongs to."""

    node_id: str
    symbol: str
    path: str
    change_type: str
    target_sha: str = ""
    target_index: int = -1
    target_subject: str = ""
    reason: str = ""

    @property
    def routed(self) -> bool:
        return bool(self.target_sha)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id, "symbol": self.symbol, "path": self.path,
            "change_type": self.change_type, "target": self.target_sha[:12],
            "target_index": self.target_index, "target_subject": self.target_subject,
            "reason": self.reason, "routed": self.routed,
        }


@dataclass
class Plan:
    #: The trunk this plan was computed against. Carried rather than
    #: re-detected: ``apply`` needs the SAME answer ``plan`` used, and
    #: re-running detection gave a different one — in a repo whose local branch
    #: IS a trunk candidate, merge-base(main, HEAD) is HEAD, the stack reads as
    #: empty, and the rebase base silently became HEAD~1. The fixups were then
    #: created and never squashed: exit 0, stack one commit taller, nothing to
    #: see unless a test asserts the height.
    trunk: str = ""
    routings: List[Routing] = field(default_factory=list)
    #: files whose nodes route to MORE than one commit — see `split_files`
    conflicted: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def by_target(self) -> Dict[str, List[Routing]]:
        out: Dict[str, List[Routing]] = {}
        for r in self.routings:
            if r.routed:
                out.setdefault(r.target_sha, []).append(r)
        return out

    def to_dict(self) -> dict:
        return {
            "routings": [r.to_dict() for r in self.routings],
            "conflicted_files": self.conflicted,
            "notes": self.notes,
        }


def _git(repo: Optional[Path], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo) if repo else None, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )


def _blob(repo: Optional[Path], rev: str, path: str) -> Optional[bytes]:
    proc = subprocess.run(
        ["git", "show", f"{rev}:{path}"], cwd=str(repo) if repo else None,
        capture_output=True,
    )
    return proc.stdout if proc.returncode == 0 else None


#: A line longer than this means the file is minified or bundled.
#:
#: This is the filter that decides whether `absorb` is usable at all. Measured
#: on this repo: scanning 50 pending files never finished in 100 s, and the
#: cause was ONE file — `bonsai-worker.js`, an esbuild bundle whose longest
#: line is 37,906 characters. tree-sitter's cost is superlinear in the size of
#: a single expression, so a bundle is not "a bit slower", it is unbounded.
#: Nineteen files before it each took under half a second.
#:
#: Ten of the 387 candidates here are like that, the worst carrying a 208,985
#: character line. They are build artifacts: they have no meaningful node
#: identity, nobody reviews them, and absorbing an edit "into" one is
#: meaningless — so skipping them costs nothing and is the honest answer, not
#: a workaround.
#:
#: Line length rather than file size, because size does not predict this: a
#: 0.53 MB hand-written Python file parses in 0.26 s, and a 0.20 MB bundle does
#: not finish. The tell is the shape, not the bulk.
MAX_LINE_BYTES = 2000

#: Absolute size guard for the pathological case a long-line check misses.
MAX_FILE_BYTES = 2_000_000


def looks_generated(data: bytes) -> bool:
    """Is this a bundle/minified artifact rather than source someone wrote?"""
    if len(data) > MAX_FILE_BYTES:
        return True
    for line in data.split(b"\n"):
        if len(line) > MAX_LINE_BYTES:
            return True
    return False


def has_node_identity(path: str) -> bool:
    """Can awgit extract nodes from this file at all?

    Filtering here is the difference between a usable command and one that
    hangs. Measured on this repo: 2477 pending files, of which **383** are
    parseable and **1556 are .xlsx**. Every unparseable file still cost a
    ``git show`` subprocess plus a parse attempt to produce zero nodes — about
    75 ms each, roughly 186 s of work to learn nothing.
    """
    if path.endswith(".py"):
        return True
    try:
        from awgit.repowise_parser import available, language_for

        usable, _ = available()
        return bool(usable and language_for(path))
    except ImportError:
        return False


def pending_files(repo: Optional[Path] = None, parseable_only: bool = True) -> List[str]:
    """Files with uncommitted changes (staged or not), excluding deletions."""
    proc = _git(repo, "status", "--porcelain")
    out: List[str] = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        status, path = line[:2], line[3:].strip().strip('"')
        if "D" in status:
            continue
        if parseable_only and not has_node_identity(path):
            continue
        out.append(path)
    return out


def node_owners(entries, repo: Optional[Path] = None,
                wanted: Optional[set] = None,
                max_commits: int = MAX_OWNER_WALK) -> Dict[str, object]:
    """node_id -> the LAST stack commit that changed it.

    Walks NEWEST-FIRST and stops as soon as every wanted node has an owner.
    That ordering is not a micro-optimisation, it is what makes the command
    usable: the first version walked the whole stack bottom-to-top, and on this
    repo's 257-commit branch `absorb` did not finish in two minutes, because
    every commit with no op-log record costs a diff-tree plus a parse of each
    file it touched. Newest-first answers the same question — the LAST commit
    to touch a node — while typically reading a handful of commits.

    ``max_commits`` bounds the walk. When it truncates, the caller SAYS so:
    a silent cap would report "this is new work" for a node whose owner was
    simply out of range, and absorb would leave it behind while looking correct.
    """
    from awgit.capture import diff_sources, load_node_manager
    from awgit.data_root import vcs_data_root
    from awgit.oplog import OpLog

    log = OpLog()
    manager = load_node_manager(vcs_data_root())
    owners: Dict[str, object] = {}
    remaining = set(wanted) if wanted is not None else None

    # FIRST, ask the op-log as an INDEX rather than replaying commits. The
    # op-log already records which nodes each commit changed, so a node's owner
    # is a lookup, not a search. Walking commits instead was the difference
    # between 2.5 minutes and a second here: with hundreds of pending nodes the
    # walk can never early-exit, so it re-derives the entire stack's node sets
    # to answer questions the log had already answered.
    if remaining:
        by_sha = {entry.sha: entry for entry in entries}
        try:
            all_ops = log.all_ops()
        except Exception:  # noqa: BLE001 - fall through to the walk
            all_ops = []
        for op in sorted(all_ops, key=lambda o: o.ts):
            entry = by_sha.get(op.git_sha)
            if entry is None:
                continue
            for nc in op.node_changes:
                if nc.node_id in remaining or nc.node_id not in owners:
                    # ascending ts, so a later op legitimately overwrites
                    owners[nc.node_id] = entry
                    remaining.discard(nc.node_id)

    for entry in reversed(entries[-max_commits:] if max_commits else entries):
        if remaining is not None and not remaining:
            break
        node_ids: List[str] = []
        ops = []
        try:
            ops = log.ops_for_commit(entry.sha)
        except Exception:  # noqa: BLE001 - a missing op-log is a slower path, not an error
            ops = []
        if ops:
            for op in ops:
                node_ids += [nc.node_id for nc in op.node_changes]
        else:
            # No op-log record (commit predates the hooks, or another client
            # made it). Compute it — degrade to slower, never to wrong.
            proc = _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r",
                        entry.sha)
            for path in [p for p in proc.stdout.splitlines() if p.strip()]:
                before = _blob(repo, f"{entry.sha}~1", path)
                after = _blob(repo, entry.sha, path)
                try:
                    node_ids += [nc.node_id for nc in
                                 diff_sources(before, after, path, manager)]
                except Exception:  # noqa: BLE001 - unparseable file, see capture
                    continue
        for node_id in node_ids:
            # Newest-first, so the FIRST commit seen to touch a node is the last
            # one that did. Never overwrite.
            if node_id not in owners:
                owners[node_id] = entry
                if remaining is not None:
                    remaining.discard(node_id)
    return owners


def plan(repo: Optional[Path] = None, trunk: str = "",
         depth: Optional[int] = None, paths: Optional[List[str]] = None,
         scan_all: bool = False) -> Plan:
    """Where each pending node change belongs."""
    from awgit import stack as stackmod
    from awgit.capture import diff_sources, load_node_manager
    from awgit.data_root import vcs_data_root

    entries = stackmod.load(repo, trunk)
    result = Plan(trunk=stackmod.detect_trunk(repo, trunk) or trunk)
    if not entries:
        result.notes.append("the stack is empty — nothing to absorb into")
        return result
    manager = load_node_manager(vcs_data_root())

    # Scan the PENDING changes first, so the ownership walk knows what it is
    # looking for and can stop as soon as it has found them all.
    candidates = pending_files(repo)
    if paths:
        wanted_paths = {p.replace("\\", "/") for p in paths}
        candidates = [c for c in candidates if c.replace("\\", "/") in wanted_paths]
    if not scan_all and len(candidates) > MAX_PENDING_FILES:
        result.notes.append(
            f"{len(candidates)} pending files carry node identity; only the first "
            f"{MAX_PENDING_FILES} were scanned. Scope with --paths, or --all to "
            f"scan every one (slow: each file costs a parse).")
        candidates = candidates[:MAX_PENDING_FILES]

    pending: List[Tuple[str, list]] = []
    generated: List[str] = []
    for path in candidates:
        try:
            work = (Path(repo or ".") / path).read_bytes()
        except OSError:
            continue
        # Check the SHAPE before paying for a blob fetch or a parse. This is
        # the guard that keeps one bundled file from stalling the command.
        if looks_generated(work):
            generated.append(path)
            continue
        head = _blob(repo, "HEAD", path)
        try:
            pending.append((path, diff_sources(head, work, path, manager)))
        except Exception as exc:  # noqa: BLE001 - unparseable: report, do not guess
            result.notes.append(f"{path}: skipped ({exc})")
    if generated:
        shown = ", ".join(generated[:3])
        more = f" (+{len(generated) - 3} more)" if len(generated) > 3 else ""
        result.notes.append(
            f"skipped {len(generated)} generated/minified file(s): {shown}{more}. "
            f"They are build artifacts with no node identity — and one of them "
            f"will stall a parser indefinitely.")

    wanted = {nc.node_id for _, changes in pending for nc in changes}
    if not wanted:
        result.notes.append("no pending node changes")
        return result
    limit = depth or MAX_OWNER_WALK
    if len(entries) > limit:
        result.notes.append(
            f"stack is {len(entries)} commits; only the newest {limit} were "
            f"searched for owners. A node owned by an older commit is reported as "
            f"new work — raise the bound with --depth if that is wrong.")
    owners = node_owners(entries, repo, wanted=wanted, max_commits=limit)

    per_file: Dict[str, set] = {}
    for path, changes in pending:
        for nc in changes:
            owner = owners.get(nc.node_id)
            routing = Routing(
                node_id=nc.node_id, symbol=nc.symbol or "", path=path,
                change_type=nc.change_type,
            )
            if owner is None:
                routing.reason = ("no commit in the stack has touched this node — "
                                  "it is new work, commit it normally")
            else:
                routing.target_sha = owner.sha
                routing.target_index = owner.index
                routing.target_subject = owner.subject
                routing.reason = "last changed by this commit"
                per_file.setdefault(path, set()).add(owner.sha)
            result.routings.append(routing)

    result.conflicted = sorted(p for p, targets in per_file.items() if len(targets) > 1)
    return result


def render(p: Plan) -> List[str]:
    lines: List[str] = []
    grouped = p.by_target()
    if not grouped:
        lines.append("awgit: nothing to absorb")
    for sha, routings in grouped.items():
        first = routings[0]
        lines.append(f"  -> {first.target_index}  {sha[:12]}  {first.target_subject[:52]}")
        for r in routings:
            lines.append(f"       {r.change_type:<18} {r.symbol or r.node_id[:12]}"
                         f"  ({r.path})")
    unrouted = [r for r in p.routings if not r.routed]
    if unrouted:
        lines.append("  -- not absorbed (new work):")
        for r in unrouted:
            lines.append(f"       {r.symbol or r.node_id[:12]}  ({r.path})")
    for path in p.conflicted:
        lines.append(f"  !! {path}: nodes route to MORE than one commit — "
                     f"absorb applies whole files, so this one is skipped")
    for note in p.notes:
        lines.append(f"  note: {note}")
    return lines


def apply(p: Plan, repo: Optional[Path] = None) -> Tuple[bool, List[str]]:
    """Create one ``--fixup`` per target commit, then autosquash.

    Application is FILE-level even though routing is node-level, and that limit
    is stated rather than hidden: a file whose nodes route to two different
    commits is skipped and named. Writing a partial file into the index would
    mean reconstructing it from node bodies, and getting that subtly wrong
    produces a commit that compiles and is missing a line — the failure mode
    this repo already names as the reason hand-filtering hunks is banned.
    """
    messages: List[str] = []
    grouped = p.by_target()
    if not grouped:
        return False, ["nothing to absorb"]

    skip = set(p.conflicted)
    staged_any = False
    # Newest target first so each fixup commit is created against a clean index.
    for sha, routings in sorted(grouped.items(),
                                key=lambda kv: -kv[1][0].target_index):
        paths = sorted({r.path for r in routings} - skip)
        if not paths:
            continue
        add = _git(repo, "add", "--", *paths)
        if add.returncode != 0:
            return False, [f"git add failed: {add.stderr.strip()}"]
        # Hooks RUN on the fixup. Skipping verification here would exempt the
        # repository's own pre-commit checks (a secret scanner, say) from
        # content that is then squashed into history — and rebase does not run
        # pre-commit either, so the scan would never see it at all.
        commit = _git(repo, "commit", f"--fixup={sha}")
        if commit.returncode != 0:
            return False, [f"git commit --fixup failed: "
                           f"{(commit.stderr or commit.stdout).strip()}"]
        staged_any = True
        messages.append(f"fixup! {sha[:12]}  ({', '.join(paths)})")

    if not staged_any:
        return False, ["every changed file routes to more than one commit; "
                       "nothing applied"]

    # Rebase from just below the LOWEST target, so every fixup has its target
    # in range. Going from HEAD~1 would leave fixups for older commits stranded
    # as real commits — which looks like it worked and is not what was asked.
    lowest = min(r.target_index for r in p.routings if r.routed)
    lowest_sha = _sha_at(repo, lowest, p.trunk)
    if not lowest_sha:
        return False, messages + [
            "could not locate the rebase base — the fixups exist but were NOT "
            "squashed; finish with `git rebase -i --autosquash`"]
    onto = f"{lowest_sha}~1"
    # -i is REQUIRED for --autosquash. Without it git accepts the flag, exits 0,
    # and silently does not squash: the fixup! commits are simply left in the
    # history as ordinary commits. The rebase reports success and the stack is
    # one commit taller than it should be, which is a wrong result that reads
    # as a working command — caught here only because the test asserts the
    # stack HEIGHT rather than the exit code.
    #
    # sequence.editor=true then accepts the generated todo list unchanged;
    # without it the interactive rebase opens an editor and hangs any
    # non-interactive caller.
    rebase = _git(repo, "-c", "sequence.editor=true", "rebase", "-i",
                  "--autosquash", "--autostash", onto)
    if rebase.returncode != 0:
        messages.append("the fixups were created but the autosquash rebase failed; "
                        "finish it with `git rebase --continue` or abort with "
                        "`git rebase --abort`")
        messages.append((rebase.stderr or rebase.stdout).strip())
        return False, messages
    messages.append("absorbed")
    return True, messages


def _sha_at(repo: Optional[Path], index: int, trunk: str = "") -> str:
    """The sha of stack entry ``index``, or "" — reloaded AFTER the fixups."""
    from awgit import stack as stackmod

    entries = stackmod.load(repo, trunk)
    for entry in entries:
        if entry.index == index:
            return entry.sha
    return ""
