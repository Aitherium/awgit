"""CLI entrypoint for the semantic-VCS layer (`python -m lib.awgit.cli`).

Used by git hooks (capture, lease-check) and by agents (status, diff, lease).
Runs as its own sync process — no event loop is active, so the blocking file
locks in the stores never reach an event loop.

Actor attribution prefers an explicit ``--actor``, then ``AITHER_ACTOR``, then
the VERIFIED GitHub login (the Aitherium GitHub OAuth-app identity via ``gh``,
cached), then the commit author. Every op records ``actor_verified`` /
``verified_actor`` so a self-asserted agent name is never mistaken for verified
identity — the verified half is the authoritative attribution.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from awgit.capture import capture_ops
from awgit.diff import diff_git, render
from awgit.git import PASSTHROUGH as PASSTHROUGH_VERBS
from awgit.leases import LeaseConflictError, LeaseRegistry, coverage_gap
from awgit.merge import list_conflicts, merge_ops, resolve_conflict
from awgit.oplog import OpLog

#: Commands main() handles BEFORE argparse (see the intercept in main()).
#: Any option their subparser declares must be parsed BY the intercept —
#: otherwise it is forwarded raw to git, which rejects it. Asserted by
#: check_awgit_cli_contract ACC006.
INTERCEPTED_COMMANDS = ("git", "commit", *PASSTHROUGH_VERBS)

#: Commands that REWRITE history. Every one must consult awgit.guard before
#: touching a commit — in a shared worktree a rebase can rewrite a peer's work.
#: Asserted by check_awgit_stack_safety ASF001, which reads this tuple, so a
#: new rewriting command cannot be added without either wiring the guard or
#: making the gate go red.
REWRITING_COMMANDS = ("uncommit", "restack", "pull", "absorb")


def _actor(args: argparse.Namespace) -> str:
    """Who is committing. MUST differ per concurrent agent, or leases are useless.

    Explicit forms win, then one is DERIVED. Deriving is the point: with only
    ``AITHER_ACTOR`` this returned "unknown", and ``lease-check`` rejects an
    unknown actor — so turning enforcement on would have blocked every commit on
    the box until every session remembered to export a variable. A guard that can
    only be switched on by breaking the machine never gets switched on, and it
    never was: measured 2026-08-09, ``awgit lease list`` had never returned a
    single lease while a concurrent session overwrote one file four times, the
    last overwrite silently reverting a fix already committed to origin.

    ``CLAUDE_CODE_SESSION_ID`` is the right derivation: the harness sets it per
    session, it is stable for that session's life, and it is inherited by the git
    hook's subprocess (verified). A per-PID value would NOT do — every ``awgit``
    invocation is a new process, so a lease taken by one command would not match
    the next.

    ``user@host`` is the last resort, for humans and other tooling. It is
    deliberately not what agents get: every session here shares one OS account
    and one GitHub identity, so a shared actor makes session A's lease cover
    session B's commit — all of the friction, none of the protection.
    """
    explicit = getattr(args, "actor", None) or os.environ.get("AITHER_ACTOR")
    if explicit:
        return explicit
    session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if session:
        return f"claude:{session}"
    user = os.environ.get("USERNAME") or os.environ.get("USER")
    if user:
        return f"{user}@{platform.node() or 'localhost'}"
    return "unknown"


def _cmd_capture(args: argparse.Namespace) -> int:
    try:
        op = capture_ops(
            args.sha,
            actor=args.actor,
            data_root=Path(args.data_root) if args.data_root else None,
        )
    except ValueError as exc:
        print(f"vcs: {exc}", file=sys.stderr)
        return 2
    if op is None:
        print(f"vcs: no semantic changes for {args.sha}")
        return 0
    print(f"vcs: op {op.op_id} recorded ({op.summary})")

    # Record proof of verification if requested
    if getattr(args, "prove", False):
        try:
            from awgit.outcomes import record_outcome
            from awgit.prove import run_gates
            data_root = Path(args.data_root) if args.data_root else None
            gates = run_gates(op.file_paths)
            outcome = record_outcome(args.sha, gates, data_root=data_root)
            print(f"vcs: outcome {outcome.outcome_id} recorded ({outcome.verdict})")
        except Exception as exc:
            print(f"vcs: proof recording failed (non-fatal): {exc}", file=sys.stderr)

    return 0


def _cmd_data(args: argparse.Namespace) -> int:
    """Row-level diff of two tabular files (see awgit/tabular.py)."""
    import json  # function-local, matching the convention in this module

    from . import tabular

    if args.data_cmd != "diff":  # pragma: no cover - argparse enforces this
        print(f"awgit: unknown data subcommand {args.data_cmd!r}", file=sys.stderr)
        return 2

    try:
        d = tabular.diff_files(args.old, args.new, args.key)
    except tabular.UnreadableTableError as exc:
        # Exit 2, never 0-with-empty-output: a table we could not read must not
        # be reported as a table with no differences.
        print(f"awgit: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps({
            "summary": d.summary(),
            "added": d.added,
            "removed": d.removed,
            "modified": [{"before": b, "after": a} for b, a in d.modified],
        }, indent=2, default=str))
        return 0

    s = d.summary()
    if d.keyless:
        print("no --key given: content set-diff only; MODIFIED rows cannot be "
              "distinguished from an add plus a remove.")
    else:
        print(f"keyed on: {', '.join(d.keys)}")
    for col in s["columns_added"]:
        print(f"  + column {col}")
    for col in s["columns_removed"]:
        print(f"  - column {col}")
    print(f"  {s['added']} added, {s['removed']} removed, "
          f"{s['modified']} modified, {s['unchanged']} unchanged")
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    try:
        changes = diff_git(args.a, args.b)
    except ValueError as exc:
        print(f"vcs: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "as_json", False):
        import json

        print(json.dumps({"count": len(changes),
                          "changes": [c.to_dict() for c in changes]}, indent=2))
        return 0
    if not changes:
        print("vcs: no semantic changes")
        return 0
    for line in render(changes):
        print(line)
    return 0


def _cmd_merge_preview(args: argparse.Namespace) -> int:
    op_a = capture_ops(args.a, actor=args.actor)
    op_b = capture_ops(args.b, actor=args.actor)
    a_ops = [op_a] if op_a else []
    b_ops = [op_b] if op_b else []
    result = merge_ops(a_ops, b_ops)
    if getattr(args, "as_json", False):
        import json

        print(json.dumps({
            "status": result.status,
            "notes": list(result.notes),
            "conflicts": [c.to_dict() for c in result.conflicts],
        }, indent=2))
        return 0 if result.status != "conflict" else 1
    print(f"vcs: merge status: {result.status}")
    for note in result.notes:
        print(f"  {note}")
    for c in result.conflicts:
        print(f"  CONFLICT {c.node_id} ({c.symbol}) — escalate for human review")
    return 0 if result.status != "conflict" else 1


def _cmd_merge_conflicts(args: argparse.Namespace) -> int:
    conflicts = list_conflicts()
    if getattr(args, "as_json", False):
        import json

        print(json.dumps({"count": len(conflicts),
                          "conflicts": [c.to_dict() for c in conflicts]}, indent=2))
        return 0
    for c in conflicts:
        print(f"{c.conflict_id} {c.node_id} {c.symbol} [{c.status}]")
    return 0


def _cmd_resolve_conflict(args: argparse.Namespace) -> int:
    if not args.body:
        print("vcs: --body <file> required", file=sys.stderr)
        return 2
    try:
        body = Path(args.body).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"vcs: cannot read {args.body}: {exc}", file=sys.stderr)
        return 2
    resolver = args.resolver or os.environ.get("AITHER_ACTOR") or "unknown"
    op = resolve_conflict(args.conflict_id, resolved_body=body, resolver=resolver)
    if op is None:
        print(f"vcs: conflict {args.conflict_id} not found", file=sys.stderr)
        return 1
    print(f"vcs: conflict resolved; synthetic op {op.op_id} recorded")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    log = OpLog()
    ops = log.all_ops()
    last = max(ops, key=lambda o: o.ts) if ops else None
    if getattr(args, "as_json", False):
        import json

        print(json.dumps({
            "ops": len(ops),
            "oplog": str(log.path),
            "last": None if last is None else {
                "ts": last.ts, "actor": last.actor, "summary": last.summary,
                "git_sha": last.git_sha, "op_id": last.op_id,
            },
        }, indent=2))
        return 0
    print(f"vcs: {len(ops)} ops in {log.path}")
    if last is not None:
        print(f"  last: {last.ts} {last.actor} {last.summary}")
    return 0


def _resolve_lease_args(registry, who, raw):
    """Map release/heartbeat arguments to lease ids, accepting leased PATHS too.

    `acquire` takes paths and `release` takes ids, so passing the same string to
    both is the natural mistake -- and it used to be a silent one, because an
    unmatched id simply released nothing and still printed success.

    Returns (ids, unresolved). Anything that is neither one of this actor's
    active lease ids nor one of its leased targets comes back in `unresolved`,
    so the caller can fail rather than report a count of zero.
    """
    mine = [lz for lz in registry.active_leases() if lz.actor == who]
    by_id = {lz.lease_id: lz.lease_id for lz in mine}
    by_target = {}
    for lz in mine:
        # Last writer wins is fine: releasing any lease on that path is the
        # intent, and a duplicate target for one actor is already a bug.
        by_target[str(lz.target).replace("\\", "/").strip("/")] = lz.lease_id
    ids, unresolved = [], []
    for arg in raw or []:
        if arg in by_id:
            ids.append(arg)
            continue
        key = str(arg).replace("\\", "/").strip("/")
        if key in by_target:
            ids.append(by_target[key])
            continue
        unresolved.append(arg)
    return ids, unresolved


def _cmd_lease(args: argparse.Namespace) -> int:
    registry = LeaseRegistry()
    cmd = args.lease_cmd
    who = _actor(args)
    if cmd == "contact":
        return _cmd_lease_contact(args)
    if cmd == "requests":
        return _cmd_lease_requests(args)
    if cmd == "acquire":
        targets = list(args.targets or [])
        if getattr(args, "staged", False):
            # Lease exactly what the gate will check. Without this, widening
            # coverage beyond .py would have made a routine commit a
            # per-file chore, and a gate that heavy gets routed around instead
            # of satisfied.
            from awgit.leases import is_guarded

            repo = Path(os.environ.get("VCS_REPO_ROOT", os.getcwd()))
            named = set(targets)
            staged = [f for f in _staged_files(repo) if is_guarded(f)]
            # ADOPTION: files that are staged but that this caller never named.
            # In a shared worktree they are routinely somebody else's — a peer
            # stages while you are mid-command — and leasing them is what makes
            # the pre-commit gate print OK on a sweep, because you then genuinely
            # hold a lease on their work. Measured 2026-08-10: three
            # awkit files a peer had staged seconds earlier were adopted in
            # silence, and 9 of their files landed in someone else's commit. The
            # gate CANNOT catch this at commit time — the committer's leases are
            # all valid — so it is caught here, at the moment of adoption.
            adopted = sorted(f for f in staged if f not in named)
            if adopted and not getattr(args, "adopt", False):
                print("vcs: REFUSED — these files are staged but you did not name "
                      "them:", file=sys.stderr)
                for adopted_path in adopted:
                    print("vcs:   " + adopted_path, file=sys.stderr)
                print("vcs: in a shared worktree these are routinely a PEER's "
                      "in-flight work, and leasing them makes the pre-commit gate "
                      "pass on a commit that sweeps it.", file=sys.stderr)
                print("vcs: name your own paths instead, or re-run with --adopt if "
                      "you have read that list and every file is yours.",
                      file=sys.stderr)
                return 1
            if adopted:
                # Proceeding deliberately still gets its own block: the failure
                # mode was these paths being indistinguishable from the ones the
                # caller actually asked for.
                print("vcs: ADOPTING " + str(len(adopted))
                      + " staged file(s) you did not name:")
                for adopted_path in adopted:
                    print("vcs:   + " + adopted_path)
            targets = sorted(named | set(staged))
            if not targets:
                print("vcs: nothing staged that the gate guards — no leases needed")
                return 0
        if not targets:
            print("vcs: acquire needs targets (or --staged)", file=sys.stderr)
            return 1
        try:
            leases = registry.acquire(
                who, targets, ttl_sec=args.ttl, reason=args.reason
            )
        except LeaseConflictError as exc:
            # Flush stdout FIRST. It is block-buffered when redirected to a file
            # or a pipe and stderr is not, so this line otherwise lands
            # INTERLEAVED in the middle of whatever stdout had buffered rather
            # than at the end where anyone looks. Measured 2026-08-19:
            # `lease acquire --staged --adopt` over 1776 files refused correctly
            # on a real conflict, and the one line saying why was glued onto the
            # middle of the adoption list at line 1657 of 1778 -- head and tail
            # both missed it, and a correct refusal got reported as "awgit exits
            # 1 and persists nothing". A diagnostic nobody can find is worse than
            # none: it gets diagnosed as a different bug.
            sys.stdout.flush()
            print(f"vcs: {exc}", file=sys.stderr)
            sys.stderr.flush()
            return 1
        # A lease over an ALREADY-DIRTY file captures a baseline that contains work
        # which is not yours, and `stage-mine` computes (baseline -> worktree), so it
        # cannot separate what it never saw as separate. Leasing after a peer has
        # started editing therefore looks exactly like leasing a clean file, and the
        # commit sweeps them — measured 2026-08-10, ~29 lines of a peer's in-flight
        # a peer's work landed in someone else's commit that way.
        #
        # This cannot REFUSE: the dirt is often your own (edit, then remember to
        # lease), and refusing would break the common case. So it says so, loudly,
        # once per dirty target, and names the fix.
        dirty = _dirty_targets([lz.target for lz in leases])
        for lz in leases:
            print(f"vcs: lease {lz.lease_id} {lz.target} until {lz.expires_ts}")
        if dirty:
            print("vcs: WARNING — leased with UNCOMMITTED changes already present:",
                  file=sys.stderr)
            for rel in dirty:
                print(f"vcs:   ! {rel}", file=sys.stderr)
            print("vcs: the baseline just snapshotted INCLUDES those changes, so "
                  "`awgit stage-mine` cannot tell them from yours.", file=sys.stderr)
            print("vcs: if any of it is a peer's, verify before committing: "
                  "`git diff --stat -- <path>` must match the size of YOUR edit.",
                  file=sys.stderr)
        return 0
    if cmd in ("heartbeat", "release"):
        # `release`/`heartbeat` take lease IDS. Passing a PATH -- the same string
        # `acquire` takes, and the obvious guess -- matched no id, so the registry
        # returned 0 and this printed "released 0 leases" and exited 0 while the
        # lease sat there in `lease list`. A command that reports success for
        # having done nothing is the silent-no-op class in
        # .claude/rules/security-review-patterns.md #5, and it cost a session on
        # 2026-08-16: the release "succeeded", the lease stayed held, and the next
        # edit was blocked by the caller's own lease.
        # Resolve path-shaped arguments against this actor's active leases, and
        # refuse anything that resolves to nothing rather than reporting 0.
        ids, unresolved = _resolve_lease_args(registry, who, args.ids)
        if unresolved:
            print("vcs: no active lease of yours matches: " + ", ".join(unresolved),
                  file=sys.stderr)
            print("vcs: pass a lease id or a leased path (`awgit lease list`)",
                  file=sys.stderr)
            return 1
        if cmd == "heartbeat":
            print(f"vcs: heartbeat refreshed {registry.heartbeat(who, ids)} leases")
        else:
            print(f"vcs: released {registry.release(who, ids)} leases")
        return 0
    if cmd == "list":
        for lz in sorted(registry.active_leases(), key=lambda x: x.target):
            print(f"{lz.lease_id} {lz.status} {lz.actor} {lz.kind} {lz.target} "
                  f"until {lz.expires_ts}")
        return 0
    if cmd == "sweep":
        print(f"vcs: swept {registry.sweep_expired()} expired leases")
        return 0
    return 2


def _staged_files(repo: Path) -> List[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


#: Max pathspecs per `git ls-files` call. Windows caps a command line at ~32KB
#: (CreateProcess), and these are repo-relative paths averaging well under 100
#: bytes, so 400 leaves a wide margin while keeping the call count small.
_LS_INDEX_CHUNK = 400


def _ls_index(repo: Path, index_file: str, paths: List[str]) -> dict:
    """path -> blob sha, read from a SPECIFIC index file.

    Paths are passed in CHUNKS. Windows' CreateProcess rejects a command line
    over ~32KB with WinError 206 ("The filename or extension is too long"), and
    this function is called by the pre-commit lease gate with every staged path
    in the tree. Measured 2026-08-19 on this worktree: ~2243 staged/untracked
    paths made the gate raise before it could judge anything, so EVERY commit in
    the repo was refused -- by the traceback, not by a lease decision.

    The failure named `subprocess`/`CreateProcess` and no path, so it read as a
    broken toolchain rather than "your argument list is too long", and it gets
    worse exactly as a shared tree gets busier -- i.e. it appears under load and
    vanishes on a clean checkout, which is the hardest shape to reproduce.
    """
    if not paths:
        return {}
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = index_file
    blobs = {}
    for i in range(0, len(paths), _LS_INDEX_CHUNK):
        chunk = paths[i:i + _LS_INDEX_CHUNK]
        out = subprocess.run(
            ["git", "ls-files", "--stage", "--", *chunk],
            cwd=str(repo), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env,
        ).stdout
        for line in out.splitlines():
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) >= 2 and path:
                blobs[path] = parts[1]
    return blobs


def staged_but_not_committed(repo: Path) -> List[str]:
    """Paths whose COMMITTED content differs from what the actor STAGED.

    ``git commit -- <pathspec>`` and ``git commit -a`` do not commit the index.
    They build a TEMPORARY index from the working tree and commit that, so a
    hook sees a commit that has nothing to do with what was staged. In a shared
    worktree that is a sweep: you inspect the diff, find a peer's in-flight work,
    deliberately stage only your own blob — and the pathspec form silently puts
    theirs back.

    Measured 2026-08-10, and this is the FOURTH recurrence of the sweep class
    (three times before) but the first by this mechanism: awgit's
    ALREADY-DIRTY warning fired correctly, the committer verified with
    `git diff --stat`, found the peer's ~39 lines, and staged a hand-built blob
    containing only their own 15 — and `git commit -- <path>` discarded it. Every
    existing guard did its job; the trap is between the staging and the commit,
    which is why nothing could see it.

    Deliberately narrow, because a rule that floods gets switched off. It fires
    ONLY when a path was really staged (index differs from HEAD) *and* the
    content about to be committed differs from that staged content. A plain
    pathspec commit over an unstaged file — the overwhelmingly common case —
    says nothing.
    """
    temp_index = os.environ.get("GIT_INDEX_FILE", "").strip()
    if not temp_index:
        return []
    # `git rev-parse --git-path index` HONOURS GIT_INDEX_FILE and hands back the
    # temporary index — so asking git where the real index is, from inside the
    # hook, returns the very file we are trying to distinguish it from. Ask for
    # the git dir with the variable stripped instead, and join it ourselves.
    clean_env = {k: v for k, v in os.environ.items() if k != "GIT_INDEX_FILE"}
    git_dir = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=clean_env,
    ).stdout.strip()
    if not git_dir:
        return []
    real_index = str(Path(git_dir) / "index")
    try:
        if Path(temp_index).resolve() == Path(real_index).resolve():
            return []  # a normal `git commit` — the index IS what is committed
    except OSError:
        return []

    # The paths AT RISK are the ones really staged — `_staged_files` would read
    # the temp index here, for the same GIT_INDEX_FILE reason as above.
    paths = [
        ln for ln in subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(repo), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=clean_env,
        ).stdout.splitlines() if ln.strip()
    ]
    if not paths:
        return []
    committing = _ls_index(repo, temp_index, paths)
    staged = _ls_index(repo, real_index, paths)

    # Chunked for the same reason as _ls_index: this is the SIBLING call on the
    # same path list, and fixing only one of them would leave the gate failing
    # at the next line under exactly the load that triggered it.
    head_blobs: dict = {}
    for i in range(0, len(paths), _LS_INDEX_CHUNK):
        out = subprocess.run(
            ["git", "ls-tree", "-r", "HEAD", "--", *paths[i:i + _LS_INDEX_CHUNK]],
            cwd=str(repo), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        ).stdout
        for line in out.splitlines():
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) >= 3 and path:
                head_blobs[path] = parts[2]

    conflicted = []
    for path in sorted(paths):
        mine = staged.get(path)
        theirs = committing.get(path)
        if mine is None or theirs is None:
            continue
        if mine == head_blobs.get(path):
            continue  # nothing was really staged for this path
        if theirs == head_blobs.get(path):
            # This commit records NO change for the path, so nothing of the
            # staged content is being swept — it simply is not being committed.
            # That is the legitimate isolated-index workflow (commit my files
            # from a temp index while a peer's work sits staged in the shared
            # one), and flagging it would leave no safe way to commit at all.
            continue
        if mine != theirs:
            conflicted.append(path)
    return conflicted


def _cmd_stage_mine(args: argparse.Namespace) -> int:
    """Stage only this actor's edits, and refuse if any of them would be lost."""
    from awgit.staging import StagingError, stage_mine, verify_staged

    if getattr(args, "self_test", False):
        from awgit.staging_selftest import run_self_test
        return run_self_test()

    repo = Path(os.environ.get("VCS_REPO_ROOT", os.getcwd()))
    who = _actor(args)
    registry = LeaseRegistry()
    held = {lz.target: lz for lz in registry.leases_by_actor(who) if lz.status == "active"}

    failed = False
    for rel in args.paths:
        rel = rel.replace("\\", "/")
        lease = held.get(rel)
        if lease is None:
            # Without a lease there is no baseline, and without a baseline "your
            # edits" is a guess. Refusing is the whole point of the command.
            print(
                f"vcs: {rel}: no active lease for {who!r} — take it BEFORE editing "
                f"(awgit lease acquire {rel})",
                file=sys.stderr,
            )
            failed = True
            continue
        try:
            result = stage_mine(rel, lease.baseline_blob, repo, dry_run=args.dry_run)
        except StagingError as exc:
            print(f"vcs: {exc}", file=sys.stderr)
            failed = True
            continue

        if result.missing:
            print(f"vcs: {rel}: {result.note}", file=sys.stderr)
            for line in result.missing[:8]:
                print(f"       lost: {line[:100]}", file=sys.stderr)
            failed = True
            continue

        print(f"vcs: {rel}: {result.note}")

        if result.staged and args.require:
            absent = verify_staged(rel, args.require, repo)
            if absent:
                # This is the assertion that would have caught the 2026-08-10
                # dropped-registration bug: the function was staged, the line
                # wiring it was not, and everything else looked correct.
                print(
                    f"vcs: {rel}: REQUIRED text missing from the STAGED copy — "
                    f"your change is staged incomplete:",
                    file=sys.stderr,
                )
                for needle in absent:
                    print(f"       missing: {needle[:100]}", file=sys.stderr)
                failed = True
    return 1 if failed else 0


def _merge_hand_resolved(repo: Path, staged: List[str]) -> List[str]:
    """During a merge, the files the COMMITTER actually decided.

    A merge commit legitimately stages everything the other lineage touched. On
    this repo that is 1,266 files for a single recovery merge, and demanding a
    lease on each is not a safety property -- it is a wall. Measured 2026-08-19:
    `lease acquire --staged --adopt` granted zero, and acquiring them in batches
    ran past six minutes without finishing, so a legitimate merge could not be
    recorded at all.

    The gate exists to stop one session sweeping another's IN-FLIGHT edit. A file
    taken verbatim from either parent is nobody's in-flight edit -- git chose it,
    not the committer. Only a file whose staged blob differs from BOTH parents was
    hand-resolved, and those are exactly the committer's own work, so those are
    what still require a lease.

    Returns the staged paths when this is not a merge, so the normal path is
    unchanged.
    """
    git_dir = subprocess.run(
        ["git", "rev-parse", "--git-dir"], cwd=str(repo),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout.strip()
    if not git_dir or not (Path(repo) / git_dir / "MERGE_HEAD").exists()             and not Path(git_dir, "MERGE_HEAD").exists():
        return staged

    def _differs(ref: str) -> set:
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only", ref], cwd=str(repo),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout
        return {ln for ln in out.splitlines() if ln.strip()}

    ours, theirs = _differs("HEAD"), _differs("MERGE_HEAD")
    if not ours and not theirs:
        # Could not read either parent -> judge nothing away; fall back to the
        # full staged set rather than exempting everything.
        return staged
    hand = sorted(set(staged) & ours & theirs)
    print(f"vcs: merge in progress -- {len(staged)} staged, {len(hand)} hand-resolved; "
          f"a lease is required on the hand-resolved files only")
    return hand


def _requests_store():
    """(LeaseRequests, registry) or (None, None) if the plane is unreadable."""
    try:
        from .lease_requests import LeaseRequests
        from .leases import LeaseRegistry, vcs_data_root
        reg = LeaseRegistry()
        return LeaseRequests(vcs_data_root()), reg
    except Exception as exc:            # noqa: BLE001 - never fatal
        print(f"vcs: lease-request store unavailable ({exc})", file=sys.stderr)
        return None, None


def _relay_notify(to_actor: str, target: str, message: str) -> bool:
    """Best-effort async ping. NEVER the only delivery path.

    Returns False when it could not send, so the caller can say so rather than
    implying the holder was reached. Relay is optional here on purpose: if the
    only notification depended on a running service, this feature would be
    unavailable in exactly the degraded conditions that produce lease pile-ups.
    """
    try:
        from awrelay.client import RelayClient       # type: ignore
    except ImportError:
        return False
    try:
        who = to_actor.split(":")[-1][:8]
        # Build the reason OUTSIDE the f-string. A replacement field spanning
        # two physical lines is PEP 701 (3.12+), and this package declares
        # requires-python = ">=3.10" -- so the previous spelling was a
        # SyntaxError for anyone pip-installing it on the oldest Python we
        # promise to support.
        why = message or "another session is blocked on it"
        RelayClient().send(
            channel="lease-negotiation",
            text=f"@{who} please release `{target}` — {why}")
        return True
    except Exception:                    # noqa: BLE001
        return False


def _cmd_lease_contact(args) -> int:
    """Ask whoever holds a path's lease to let it go."""
    store, reg = _requests_store()
    if store is None or reg is None:
        return 2
    me = _actor(args)
    target = args.path

    holder = None
    for lz in reg.active_leases():
        if lz.target == target:
            holder = lz
            break
    if holder is None:
        print(f"vcs: no active lease on {target!r} — nothing to ask for. "
              f"If a commit was refused, re-run `awgit lease acquire`.")
        return 0
    if holder.actor == me:
        print(f"vcs: {target!r} is held by YOU ({me}); "
              f"`awgit lease release {holder.lease_id}` frees it.")
        return 0

    store.add(target, holder.actor, me, getattr(args, "message", "") or "")
    relayed = _relay_notify(holder.actor, target,
                            getattr(args, "message", "") or "")
    print(f"vcs: asked {holder.actor} to release {target!r} "
          f"(expires {holder.expires_ts}).")
    print("vcs: they see it on their next `awgit lease list`"
          + (" and on relay #lease-negotiation." if relayed
             else " (relay unavailable — the awgit path still delivers)."))
    return 0


def _cmd_lease_requests(args) -> int:
    """What am I blocking, and what am I waiting on?"""
    store, _ = _requests_store()
    if store is None:
        return 2
    me = _actor(args)
    from .lease_requests import format_pending

    incoming = store.for_actor(me)
    outgoing = store.by_actor(me)
    if incoming:
        print(format_pending(incoming))
    else:
        print("vcs: nobody is blocked on leases you hold.")
    if outgoing:
        print(f"vcs: you have asked for {len(outgoing)} lease(s):")
        for r in outgoing[:10]:
            print(f"vcs:   {r.get('target')} <- {str(r.get('to_actor'))[:24]}")
    return 0


def _cmd_lease_check(args: argparse.Namespace) -> int:
    if os.environ.get("VCS_LEASES_ENFORCE", "0") != "1":
        print("vcs: lease-check not enforced (VCS_LEASES_ENFORCE=0)")
        return 0
    repo = Path(os.environ.get("VCS_REPO_ROOT", os.getcwd()))
    who = _actor(args)
    if who == "unknown":
        print("vcs: lease-check requires AITHER_ACTOR (or --actor)", file=sys.stderr)
        return 1
    gap = coverage_gap(_merge_hand_resolved(repo, _staged_files(repo)), who)
    if gap:
        print(
            "vcs: commit rejected — no active lease covering: " + ", ".join(gap),
            file=sys.stderr,
        )
        return 1
    overridden = staged_but_not_committed(repo)
    if overridden:
        print(
            "vcs: commit rejected — you STAGED one thing and are committing another:\n"
            + "".join(f"vcs:   ! {p}\n" for p in overridden)
            + "vcs: `git commit -- <pathspec>` and `git commit -a` build a TEMPORARY\n"
            "vcs: index from the WORKING TREE and ignore what you staged. In a shared\n"
            "vcs: worktree that silently re-adds a peer's in-flight edits you had\n"
            "vcs: deliberately excluded. Commit the index instead: `git commit` with\n"
            "vcs: no pathspec, or re-stage and drop the pathspec.",
            file=sys.stderr,
        )
        return 1
    print("vcs: lease-check OK")
    return 0



def _cmd_graph(args: argparse.Namespace) -> int:
    from awgit.graph import build, to_json, to_mermaid

    g = build(since=args.since, actor=args.actor)
    # --json is the uniform read contract; --format json is the graph-specific
    # spelling that predates it. Either wins over mermaid.
    want_json = args.format == "json" or getattr(args, "as_json", False)
    text = to_json(g) if want_json else to_mermaid(g)
    if args.out:
        try:
            Path(args.out).write_text(text, encoding="utf-8")
        except OSError as exc:
            print(f"vcs: cannot write {args.out}: {exc}", file=sys.stderr)
            return 2
        print(f"vcs: wrote {args.format} graph to {args.out} "
              f"({g['ops']} ops, {len(g['collisions'])} collision(s))")
    else:
        print(text)
    return 0



def _cmd_evidence(args: argparse.Namespace) -> int:
    from awgit.evidence import gather, render, to_json

    ev = gather(since=args.since)
    print(to_json(ev) if args.json else render(ev))
    return 0


def _cmd_bodies(args: argparse.Namespace) -> int:
    from awgit.bodies import BodyStore

    store = BodyStore()
    if not args.get:
        st = store.stats()
        print(f"vcs: body store: {st['blobs']} blobs, {st['bytes']:,} bytes")
        return 0
    body = store.get(args.get)
    if body is None:
        print(f"vcs: no body for {args.get}", file=sys.stderr)
        return 1
    if args.out:
        try:
            Path(args.out).write_bytes(body)
        except OSError as exc:
            print(f"vcs: cannot write {args.out}: {exc}", file=sys.stderr)
            return 2
        print(f"vcs: wrote {len(body)} bytes to {args.out}")
    else:
        sys.stdout.write(body.decode("utf-8", errors="replace"))
    return 0


def _cmd_dedupe(args: argparse.Namespace) -> int:
    from awgit.bodies import BodyStore, dedupe_report, op_referenced_shas

    if args.scan:
        rep = dedupe_report([Path(p) for p in args.scan])
        print(
            f"vcs: dedupe across {len(args.scan)} trees: "
            f"{rep['groups']} identical-content groups, "
            f"{rep['duplicate_files']} duplicate files, "
            f"{rep['wasted_bytes']:,} bytes duplicated"
        )
        return 0
    if args.reclaim:
        from awgit.bodies import reclaim

        res = reclaim([Path(p) for p in args.reclaim], dry_run=not args.apply)
        verb = "linked" if args.apply else "would link"
        print(
            f"vcs: reclaim {verb} {res['linked']} duplicate files across "
            f"{res['groups']} groups ({res['reclaimed_bytes']:,} bytes)"
        )
        print(
            f"vcs:   skipped {res['skipped_tracked']} git-tracked files (never linked)"
        )
        if not args.apply:
            print("vcs: dry-run — pass --apply to actually hard-link")
        return 0
    store = BodyStore()
    st = store.stats()
    if args.gc:
        referenced = op_referenced_shas()
        res = store.gc(referenced, dry_run=not args.apply)
        verb = "removed" if args.apply else "would remove"
        print(
            f"vcs: gc {verb} {res['removed']} orphaned blobs "
            f"({res['freed']:,} bytes)"
        )
        if not args.apply:
            print("vcs: dry-run — pass --apply to actually delete")
        return 0
    referenced = op_referenced_shas()
    orphaned = len(store.blob_shas() - referenced)
    print(
        f"vcs: body store: {st['blobs']} blobs, {st['bytes']:,} bytes, "
        f"{len(referenced)} referenced by op-log, {orphaned} orphaned"
    )
    if orphaned:
        print("vcs: run `awgit dedupe --gc` (dry-run) to see reclaimable space")
    return 0


def _cmd_ledger(args: argparse.Namespace) -> int:
    """Attribution view — who changed what, under a verified GitHub identity."""
    import json as _json

    from awgit.ledger import op_to_ledger_entry
    from awgit.oplog import OpLog

    ops = OpLog().all_ops()
    if args.op:
        # 🪤 `--op` used to match ONLY `op_id`, while the listing below prints
        # `ledger_ref` as its first column and the op_id NOWHERE. So the one
        # identifier the command hands you was the one identifier it refused,
        # and `awgit ledger --op <id-copied-from-awgit-ledger>` answered
        # "no ops match" — which reads as "that op does not exist" rather than
        # "you passed the wrong one of two ids you were never shown". Accept
        # either; they are both stable handles for the same op.
        # 🪤 PREFIX match, not equality. The listing abbreviates op_id to 16
        # chars, so an exact-match lookup rejects the very string it printed —
        # the identical defect one layer down, and it was reintroduced while
        # fixing the first one. Git accepts short shas for exactly this reason.
        wanted = args.op
        matches = [
            o for o in ops
            if o.op_id.startswith(wanted)
            or (op_to_ledger_entry(o).ledger_ref or "").startswith(wanted)
        ]
        if len(matches) > 1 and not any(
            o.op_id == wanted or op_to_ledger_entry(o).ledger_ref == wanted
            for o in matches
        ):
            # Ambiguity must be LOUD. Silently taking the first match is how a
            # lookup starts answering about the wrong op.
            print(
                f"vcs: ledger: '{wanted}' is ambiguous ({len(matches)} ops match) "
                "— use more characters",
                file=sys.stderr,
            )
            return 1
        ops = matches
    elif args.sha:
        ops = [o for o in ops if o.git_sha == args.sha]
    if not ops:
        print("vcs: ledger: no ops match", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        # Machine surface. The text form is lossy on purpose (it is a human
        # attribution view), so anything programmatic — a world-model seeder, a
        # reward program, an export — needs the full op rather than a re-parse
        # of a display string that was never a contract.
        entries = []
        for op in ops:
            entry = op_to_ledger_entry(op)
            d = op.to_dict()
            d["ledger_ref"] = entry.ledger_ref
            entries.append(d)
        print(_json.dumps(entries, indent=2, sort_keys=True))
        return 0

    for op in ops:
        entry = op_to_ledger_entry(op)
        verified = (
            f" (verified {entry.verified_actor})" if entry.actor_verified else ""
        )
        # op_id is printed too: it is half of what `--op` accepts, and omitting
        # it is what made the lookup unusable from this command's own output.
        print(
            f"{entry.ledger_ref} {op.op_id[:16]} {entry.actor}{verified} "
            f"{entry.git_sha[:10]} {entry.node_changes} node_changes {entry.ts}"
        )
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    import json as _json

    from awgit.sync import export_delta, import_delta, sync_status

    data_root = Path(args.data_root) if args.data_root else None
    # Normalise the verb form onto the flag form so there is ONE code path;
    # two implementations of the same command is how they come to disagree.
    sync_cmd = getattr(args, "sync_cmd", None)
    if sync_cmd == "export":
        args.export = True
    elif sync_cmd == "import":
        args.import_file = args.bundle
    if args.export:
        # export the ops a PEER is missing; default full-clone (known empty).
        known = set(args.known) if args.known else set()
        bundle = export_delta(
            known, data_root=data_root, include_bodies=not args.meta_only
        )
        out = args.out or "awgit-delta.json"
        try:
            Path(out).write_text(_json.dumps(bundle), encoding="utf-8")
        except OSError as exc:
            print(f"vcs: cannot write {out}: {exc}", file=sys.stderr)
            return 2
        print(
            f"vcs: exported {bundle['op_count']} ops, {bundle['body_count']} bodies -> {out}"
        )
        return 0
    if args.import_file:
        try:
            bundle = _json.loads(Path(args.import_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"vcs: cannot read bundle {args.import_file}: {exc}", file=sys.stderr)
            return 2
        res = import_delta(bundle, data_root=data_root)
        print(
            f"vcs: imported {res['imported_ops']} ops, {res['bodies']} bodies "
            f"(skipped {res['skipped']}); applied {res['applied']}"
        )
        return 0
    st = sync_status(data_root=data_root)
    print(
        f"vcs: sync {st['ops']} ops, {st['applied']} applied, {st['missing']} missing, "
        f"{st['bodies']} bodies ({st['bytes']:,} bytes)"
    )
    return 0


def _cmd_clone(args: argparse.Namespace) -> int:
    """Clone lazily, and report what the clone actually IS."""
    import json as _json

    from awgit import lazy

    ok, messages, status = lazy.clone(args.url, args.dest, paths=args.paths or None)
    if getattr(args, "as_json", False):
        print(_json.dumps({"ok": ok, "messages": messages,
                           "status": status.to_dict()}, indent=2))
        return 0 if ok else 1
    for message in messages:
        print(f"awgit: {message}", file=None if ok else sys.stderr)
    if ok:
        for line in lazy.render(status, lazy.measure(args.dest)):
            print(line)
    return 0 if ok else 1


def _cmd_sparse(args: argparse.Namespace) -> int:
    """Widen or inspect a sparse working tree.

    Named `sparse`, not `checkout`: `checkout` is a FORWARDED git verb and
    taking it would silently change what `awgit checkout <branch>` does for
    everyone with that muscle memory. ACC005 caught the collision from source
    before the parser could even build.
    """
    import json as _json

    from awgit import lazy

    if args.sparse_cmd == "add":
        ok, message = lazy.widen(args.paths)
        print(f"awgit: {message}", file=None if ok else sys.stderr)
        return 0 if ok else 1
    status = lazy.verify()
    if getattr(args, "as_json", False):
        print(_json.dumps({"status": status.to_dict(),
                           "sizes": lazy.measure()}, indent=2))
        return 0
    for line in lazy.render(status, lazy.measure()):
        print(line)
    return 0


def _cmd_owners(args: argparse.Namespace) -> int:
    """Declared vs MEASURED ownership. Both, because disagreement is the signal."""
    import json as _json

    from awgit import owners as ow

    decl, meas = ow.report(args.path)
    if getattr(args, "as_json", False):
        print(_json.dumps({"path": args.path, "declared": decl,
                           "measured": [o.to_dict() for o in meas]}, indent=2))
        return 0
    for line in ow.render(args.path, decl, meas):
        print(line)
    return 0


def _cmd_code(args: argparse.Namespace) -> int:
    """Where a symbol is defined, from the node registry."""
    import json as _json

    from awgit import code as cd

    rows = (cd.definitions(args.symbol) if args.code_cmd == "def"
            else cd.search(args.symbol))
    if getattr(args, "as_json", False):
        print(_json.dumps({"query": args.symbol, "count": len(rows),
                           "results": rows}, indent=2))
        return 0
    for line in cd.render(rows, args.symbol):
        print(line)
    return 0 if rows else 1


def _cmd_prove(args: argparse.Namespace) -> int:
    """Evidence for this change: nodes touched, and what each gate returned."""
    from awgit import prove as pv

    bundle = pv.build(args.sha, paths=args.paths or [])
    if getattr(args, "as_json", False):
        print(pv.to_json(bundle))
    elif args.markdown:
        print(pv.to_markdown(bundle))
    else:
        for line in pv.render(bundle):
            print(line)
    # Exit 1 on a violation, 2 when the change could not be JUDGED — a gate that
    # died, or no gate at all.
    #
    # The no-gate case exiting 0 was the first version of this, and it is
    # precisely the defect this command exists to expose: it printed "NOT
    # PROVED" and returned success, so any script gating on `awgit prove` would
    # have treated "nothing verified this" as verified. A verdict of "unproved"
    # must never be spelled the same way as "proved".
    if bundle.violations:
        return 1
    if bundle.dead or not bundle.gates:
        return 2
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    """Review threads anchored to nodes. See awgit/review.py."""
    import json as _json

    from awgit import review as rv
    from awgit.changeid import of_commit

    change_id = getattr(args, "change_id", "") or of_commit("HEAD") or ""
    if not change_id:
        print("awgit: no Change-Id on HEAD — pass --change-id", file=sys.stderr)
        return 1

    if args.review_cmd == "comment":
        if not args.node:
            print("awgit: --node <id> is required (see `awgit diff --json`)",
                  file=sys.stderr)
            return 2
        thread = rv.add_comment(change_id, args.node, args.body, _actor(args),
                                symbol=args.symbol or "", path=args.file or "")
        print(f"awgit: draft on {thread.symbol or thread.node_id[:12]} "
              f"[{thread.thread_id[:8]}] — `awgit review submit` to publish")
        return 0
    if args.review_cmd == "submit":
        published = rv.submit(change_id)
        print(f"awgit: published {len(published)} comment(s)")
        return 0
    if args.review_cmd == "resolve":
        ok = rv.resolve(change_id, args.thread_id)
        print(f"awgit: {'resolved ' + args.thread_id if ok else 'no such thread'}",
              file=None if ok else sys.stderr)
        return 0 if ok else 1

    threads = rv.load(change_id)
    if getattr(args, "as_json", False):
        print(_json.dumps({
            "change_id": change_id,
            "open": len(rv.unresolved(change_id)),
            "threads": [t.to_dict() for t in threads],
        }, indent=2))
        return 0
    for line in rv.render(threads):
        print(line)
    still_open = len(rv.unresolved(change_id))
    if still_open:
        print(f"  {still_open} open thread(s) — these block `awgit pr merge`")
    return 0


def _cmd_push(args: argparse.Namespace) -> int:
    """Publish the stack: one pull request per commit. There is no `pr create`."""
    import json as _json

    from awgit import push as pushmod

    plan = pushmod.plan(_actor(args), trunk=getattr(args, "trunk", "") or "",
                        query_github=not args.offline)
    if getattr(args, "as_json", False):
        print(_json.dumps(plan.to_dict(), indent=2))
        return 1 if plan.problems else 0
    for line in pushmod.render(plan):
        print(line)
    if plan.problems:
        return 1
    if not args.apply:
        print("awgit: dry run — pass --apply to publish")
        return 0
    ok, messages = pushmod.apply(plan)
    for message in messages:
        print(f"awgit: {message}", file=None if ok else sys.stderr)
    return 0 if ok else 1


def _cmd_pr(args: argparse.Namespace) -> int:
    """Read-side PR commands, forwarded to gh with awgit's stack context."""
    if args.pr_cmd == "create":
        print("awgit: there is no `pr create` — `awgit push` opens one PR per "
              "commit in the stack.", file=sys.stderr)
        return 2
    if args.pr_cmd == "wait":
        return _cmd_pr_wait(args)
    if args.pr_cmd == "merge":
        # Unresolved review threads BLOCK the merge. "Merge and follow up" is
        # how an objection becomes a TODO nobody files — the thread is right
        # here, and the only moment anyone is looking at it is now.
        from awgit import review as rv
        from awgit.changeid import of_commit

        change_id = of_commit("HEAD")
        open_threads = rv.unresolved(change_id) if change_id else []
        if open_threads:
            print(f"awgit: refusing to merge — {len(open_threads)} unresolved "
                  f"review thread(s):", file=sys.stderr)
            for thread in open_threads:
                last = thread.comments[-1].body if thread.comments else ""
                print(f"awgit:   {thread.symbol or thread.node_id[:12]} "
                      f"[{thread.thread_id[:8]}] {last[:50]}", file=sys.stderr)
            print("awgit: resolve them (`awgit review resolve <id>`) or say why.",
                  file=sys.stderr)
            return 1
    forward = {"list": ["pr", "list"], "view": ["pr", "view"],
               "checks": ["pr", "checks"], "merge": ["pr", "merge"]}
    args_out = forward[args.pr_cmd] + list(getattr(args, "rest", []) or [])
    return subprocess.run(["gh", *args_out]).returncode


def _cmd_pr_wait(args: argparse.Namespace) -> int:
    """Block until a PR reaches a state. Exit 0 when it does, 124 on timeout.

    124 is what `timeout(1)` returns, so an agent loop or a CI step can tell
    "it happened" from "I gave up" without parsing anything. Returning 1 for
    both would make a slow merge indistinguishable from a rejected one.
    """
    import json as _json
    import time as _time

    field = {"merged": "state", "checks": "statusCheckRollup",
             "mergeable": "mergeable"}[args.for_state]
    deadline = _time.monotonic() + args.timeout
    while True:
        proc = subprocess.run(
            ["gh", "pr", "view", str(args.number), "--json", field],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            print(f"awgit: gh pr view failed: {proc.stderr.strip()}", file=sys.stderr)
            return 2
        try:
            data = _json.loads(proc.stdout or "{}")
        except ValueError:
            data = {}
        if args.for_state == "merged" and str(data.get("state", "")).upper() == "MERGED":
            print(f"awgit: #{args.number} merged")
            return 0
        mergeable = str(data.get("mergeable", "")).upper()
        if args.for_state == "mergeable" and mergeable == "MERGEABLE":
            print(f"awgit: #{args.number} is mergeable")
            return 0
        if args.for_state == "checks":
            rollup = data.get("statusCheckRollup") or []
            states = {str(c.get("conclusion") or c.get("state") or "").upper()
                      for c in rollup}
            if rollup and not (states & {"", "PENDING", "IN_PROGRESS", "QUEUED"}):
                ok = not (states & {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT"})
                print(f"awgit: #{args.number} checks {'passed' if ok else 'FAILED'}")
                return 0 if ok else 1
        if _time.monotonic() >= deadline:
            print(f"awgit: timed out after {args.timeout}s waiting for "
                  f"{args.for_state} on #{args.number}", file=sys.stderr)
            return 124
        _time.sleep(args.interval)


def _cmd_absorb(args: argparse.Namespace) -> int:
    """Route pending changes into the commits that own those nodes."""
    import json as _json

    from awgit import absorb as absorbmod
    from awgit import guard

    plan = absorbmod.plan(trunk=getattr(args, "trunk", "") or "",
                          depth=getattr(args, "depth", None),
                          paths=getattr(args, "paths", None),
                          scan_all=getattr(args, "scan_all", False))
    if getattr(args, "as_json", False):
        print(_json.dumps(plan.to_dict(), indent=2))
        return 0
    for line in absorbmod.render(plan):
        print(line)
    if not args.apply:
        if plan.by_target():
            print("awgit: dry run — pass --apply to absorb")
        return 0
    rc = guard.require(_actor(args))
    if rc is not None:
        return rc
    ok, messages = absorbmod.apply(plan)
    for message in messages:
        print(f"awgit: {message}", file=None if ok else sys.stderr)
    return 0 if ok else 1


def _cmd_uncommit(args: argparse.Namespace) -> int:
    """Undo the last commit, keeping its changes pending."""
    from awgit import guard
    from awgit.git import run

    rc = guard.require(_actor(args))
    if rc is not None:
        return rc
    return run(["reset", "--soft", "HEAD~1"])


def _cmd_restack(args: argparse.Namespace) -> int:
    """Repair the stack after a rewrite, then rebase it onto trunk.

    ORPHAN REPAIR COMES FIRST, and it is the half that was missing. Amend a
    commit in the middle of a stack and everything above it still points at the
    commit you replaced; git does not follow. Without this, `prev` + amend +
    `restack` printed "HEAD is up to date" and silently DROPPED the rest of the
    stack — found by doing exactly that during an end-to-end push test, after a
    reviewer noticed the test was reaching for raw git instead of awgit.

    `pull` is this plus a fetch. Commits that already landed drop out by
    themselves, because git recognises them as already applied.
    """
    from awgit import guard
    from awgit import stack as stackmod
    from awgit.git import run

    rc = guard.require(_actor(args))
    if rc is not None:
        return rc

    stranded = stackmod.orphans(trunk=getattr(args, "trunk", "") or "")
    for entry in stranded:
        picked = subprocess.run(
            ["git", "cherry-pick", entry.sha], capture_output=True,
            text=True, encoding="utf-8", errors="replace")
        if picked.returncode != 0:
            print(f"awgit: replaying {entry.sha[:12]} ({entry.subject[:40]}) hit a "
                  f"conflict — resolve it, then `git cherry-pick --continue`",
                  file=sys.stderr)
            return 1
        print(f"awgit: replayed {entry.sha[:12]}  {entry.subject[:52]}")
    if stranded:
        print(f"awgit: restacked {len(stranded)} orphaned commit(s)")

    trunk = stackmod.detect_trunk(explicit=getattr(args, "trunk", "") or "")
    if not trunk:
        print("awgit: no trunk found — tried "
              + ", ".join(stackmod.TRUNK_CANDIDATES), file=sys.stderr)
        return 1
    if args.cmd == "pull":
        remote = trunk.split("/")[0] if "/" in trunk else "origin"
        rc = run(["fetch", remote])
        if rc != 0:
            return rc
    return run(["rebase", trunk])


def _cmd_stack(args: argparse.Namespace) -> int:
    """The smartlog: your commits above trunk, newest first."""
    import json as _json

    from awgit import stack as stackmod

    trunk = stackmod.detect_trunk(explicit=getattr(args, "trunk", "") or "")
    entries = stackmod.load(trunk=trunk or "")
    if getattr(args, "as_json", False):
        print(_json.dumps({
            "trunk": trunk,
            "count": len(entries),
            "head_index": stackmod.position(entries),
            "commits": [e.to_dict() for e in entries],
        }, indent=2))
        return 0
    if trunk is None:
        print("awgit: no trunk found — tried " + ", ".join(stackmod.TRUNK_CANDIDATES),
              file=sys.stderr)
        return 1
    for line in stackmod.render(entries, trunk):
        print(line)
    return 0


def _cmd_move(args: argparse.Namespace) -> int:
    """Check out the neighbouring commit in the stack (`prev` / `next`)."""
    from awgit import stack as stackmod

    entries = stackmod.load(trunk=getattr(args, "trunk", "") or "")
    if not entries:
        print("awgit: the stack is empty", file=sys.stderr)
        return 1
    step = -1 if args.cmd == "prev" else 1
    target = stackmod.neighbour(entries, step)
    if target is None:
        edge = "bottom" if step < 0 else "top"
        print(f"awgit: already at the {edge} of the stack", file=sys.stderr)
        return 1
    from awgit.git import run

    rc = run(["checkout", target.sha])
    if rc == 0:
        print(f"awgit: {target.index}  {target.sha[:12]}  {target.subject[:60]}")
    return rc


def _cmd_worktree(args: argparse.Namespace) -> int:
    """Create/list/remove worktrees — where rewrites are always safe."""
    import json as _json

    from awgit import worktree as wt

    if args.worktree_cmd == "list":
        rows = wt.listing()
        if getattr(args, "as_json", False):
            print(_json.dumps({"count": len(rows), "worktrees": [
                {"path": p, "sha": s, "branch": b} for p, s, b in rows]}, indent=2))
        else:
            for path, sha, branch in rows:
                print(f"  {sha[:12]}  {branch:<28}  {path}")
        return 0
    if args.worktree_cmd == "new":
        ok, msg, _ = wt.create(args.name, at=args.at)
        print(f"awgit: {msg}" if ok else f"awgit: {msg}",
              file=None if ok else sys.stderr)
        return 0 if ok else 1
    if args.worktree_cmd == "rm":
        ok, msg = wt.remove(args.name, force=args.force)
        print(f"awgit: {msg}", file=None if ok else sys.stderr)
        return 0 if ok else 1
    return 2


def _cmd_git_passthrough(args: argparse.Namespace) -> int:
    """Forward an allowlisted verb, or an explicit `awgit git -- ...`, to git."""
    from awgit.git import forward, run, strip_separator

    rest = strip_separator(getattr(args, "rest", []) or [])
    if args.cmd == "git":
        if not rest:
            print("awgit: usage: awgit git [--] <git args>", file=sys.stderr)
            return 2
        return run(rest)
    return forward(args.cmd, rest)


def _cmd_commit(args: argparse.Namespace) -> int:
    """git commit, with the lease gate in front and the capture behind.

    OWNED rather than forwarded because this is the one verb where awgit has
    work on both sides. The hooks already do both when they are installed; this
    keeps the behaviour when they are not, and keeps `awgit commit` honest for
    anyone who types it expecting awgit to be involved.
    """
    from awgit.git import run, strip_separator

    rest = strip_separator(getattr(args, "rest", []) or [])
    # The lease gate, for real. The pre-commit hook does this when it is
    # installed; without this the docstring's promise was half true -- capture
    # had a fallback and the GUARD did not, which is the worse half to be
    # missing, because a guard that is documented and absent reads as
    # protection. Honours VCS_LEASES_ENFORCE exactly as the hook does, so
    # `awgit commit` is never stricter than `git commit`.
    if os.environ.get("VCS_LEASES_ENFORCE") == "1":
        repo = Path(os.environ.get("VCS_REPO_ROOT", os.getcwd()))
        gap = coverage_gap(_staged_files(repo), _actor(args))
        if gap:
            print("vcs: commit rejected — no active lease covering: "
                  + ", ".join(sorted(gap)), file=sys.stderr)
            print("vcs: acquire one: awgit lease acquire <paths>", file=sys.stderr)
            return 1
    rc = run(["commit", *rest])
    if rc != 0:
        return rc
    # Capture is best-effort by contract: the commit ALREADY happened, and an
    # annotation failure must never be reported as a commit failure.
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=True,
        ).stdout.strip()
        op = capture_ops(sha, actor=_actor(args))
        if op is not None:
            print(f"vcs: op {op.op_id} recorded ({op.summary})")
    except Exception as exc:  # noqa: BLE001 - see docstring
        print(f"vcs: commit succeeded; capture skipped ({exc})", file=sys.stderr)
    return 0


def _cmd_change_id(args: argparse.Namespace) -> int:
    """Read or stamp the stable change identity. See awgit/changeid.py."""
    import json as _json

    from awgit import changeid

    if args.changeid_cmd == "ensure":
        got = changeid.ensure_in_file(Path(args.file), source=args.source or "")
        if got is None:
            print(f"vcs: change-id not stamped (source={args.source!r})")
            return 0
        print(f"vcs: {changeid.TRAILER}: {got}")
        return 0
    if args.changeid_cmd == "show":
        got = changeid.of_commit(args.rev)
        if getattr(args, "as_json", False):
            print(_json.dumps({"rev": args.rev, "change_id": got}))
        elif got:
            print(got)
        else:
            print(f"vcs: no {changeid.TRAILER} on {args.rev}", file=sys.stderr)
            return 1
        return 0
    if args.changeid_cmd == "find":
        shas = changeid.find(args.change_id)
        if getattr(args, "as_json", False):
            print(_json.dumps({"change_id": args.change_id, "commits": shas}))
        else:
            for sha in shas:
                print(sha)
        return 0 if shas else 1
    return 2


def _cmd_commands(args: argparse.Namespace) -> int:
    """Describe the CLI as data. See awgit/commands.py for why it is derived."""
    from awgit import __version__ as version
    from awgit.commands import render

    print(render(build_parser(), version, as_json=getattr(args, "as_json", False)))
    return 0


def _cmd_version(args: argparse.Namespace) -> int:
    from awgit import __version__ as version

    if getattr(args, "as_json", False):
        import json

        print(json.dumps({"tool": "awgit", "version": version}))
    else:
        print(f"awgit {version}")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    """Set awgit up in this repo: verify the actor, install the capture hooks.

    The README has advertised this command since the package was written and the
    dispatcher never had it, so `awgit init` — step one of the documented setup —
    exited 2 with "invalid choice". An advertised command that is not a real one
    reads as a broken install to whoever just ran the install line, and it is
    invisible here: the package imports, the tests pass, and the only person who
    finds out is a stranger following the README. A check now asserts that every
    command the docs name exists in the dispatcher, so it cannot come back.

    Reports rather than fails on a missing ``gh``: a verified identity is
    best-effort by design (capture never depends on the network), so an
    unauthenticated box gets a usable awgit and an honest warning, not a refusal.
    """
    from awgit.bridge import install_hooks
    from awgit.capture import _github_identity
    from awgit.data_root import vcs_data_root

    data_root = vcs_data_root()
    actor = _actor(args)
    print(f"awgit: actor          {actor}")

    login = _github_identity(data_root)
    if login:
        print(f"awgit: verified as    github:{login}")
    else:
        print("awgit: verified as    (none — `gh` missing, offline, or logged out)")
        print("awgit:                ops record a self-asserted actor until `gh auth login`")

    print(f"awgit: store          {data_root}")

    if args.no_hooks:
        print("awgit: hooks          skipped (--no-hooks)")
        return 0
    try:
        installed = install_hooks(args.repo)
    except OSError as exc:
        print(f"awgit: hooks          FAILED ({exc})", file=sys.stderr)
        print("awgit:                run inside a git repo, or pass --repo <path>",
              file=sys.stderr)
        return 1
    if not installed:
        print("awgit: hooks          none installed — is this a git repo?", file=sys.stderr)
        return 1
    for path in installed:
        print(f"awgit: hook           {path}")
    print("awgit: ready — every commit from here is captured as a semantic edit-op")
    return 0


def _cmd_hooks(args: argparse.Namespace) -> int:
    """Install/uninstall the chained capture hooks (see bridge.install_hooks)."""
    from awgit.bridge import install_hooks, uninstall_hooks

    if args.action == "install":
        installed = install_hooks(args.repo)
        for p in installed:
            print(f"vcs: installed hook {p}")
        if not installed:
            print("vcs: no hooks installed", file=sys.stderr)
            return 2
        return 0
    if args.action == "uninstall":
        for p in uninstall_hooks(args.repo):
            print(f"vcs: restored hook {p}")
        return 0
    return 2



def _dirty_targets(targets):
    """Which of these paths already carry uncommitted changes.

    Best-effort and NEVER fatal: this runs on the happy path of `lease acquire`, and a
    git hiccup must not stop someone taking a lease. Returning [] on failure is safe
    because the warning is advisory — the lease itself is unaffected.
    """
    if not targets:
        return []
    try:
        import subprocess

        proc = subprocess.run(
            ["git", "status", "--porcelain", "--", *targets],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        rel = line[3:].strip().strip('"')
        if rel:
            out.append(rel)
    return out


def build_parser() -> argparse.ArgumentParser:
    """The whole CLI, as a parser.

    Split out of ``main`` so ``awgit commands`` can DESCRIBE the CLI by walking
    the real parser rather than a hand-written list. A second, hand-maintained
    list of commands is how ``awgit init`` came to be advertised in the README
    for months without existing — deriving the description from the parser makes
    that unrepresentable.
    """
    parser = argparse.ArgumentParser(
        prog="awgit",
        description="awgit — Aither World-Graph git: semantic version control on top of git",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_capture = sub.add_parser("capture", help="capture a commit as an EditOp")
    p_capture.add_argument("--sha", required=True)
    p_capture.add_argument("--actor", default=None)
    p_capture.add_argument(
        "--data-root",
        default=None,
        help="vcs store directory (default: ~/.aither/awgit/data, or $VCS_DATA_ROOT)",
    )
    p_capture.add_argument(
        "--prove",
        action="store_true",
        help="run gates and record the outcome after capture",
    )

    p_diff = sub.add_parser("diff", help="node-level diff between two shas")
    p_diff.add_argument("a", help="base sha")
    p_diff.add_argument("b", help="target sha")
    p_diff.add_argument("--json", action="store_true", dest="as_json")

    # `data` is its own verb rather than an overload of `diff`: `awgit diff`
    # means NODE diff and keeps that meaning (the MCP handler, two skills, the
    # hooks and two blog posts all depend on its shape), so a tabular diff gets
    # its own noun instead of silently changing an existing contract.
    p_data = sub.add_parser("data", help="row-level operations on tabular files")
    data_sub = p_data.add_subparsers(dest="data_cmd", required=True)
    p_data_diff = data_sub.add_parser(
        "diff", help="row-level diff of two CSV/TSV/parquet files")
    p_data_diff.add_argument("old", help="baseline table")
    p_data_diff.add_argument("new", help="target table")
    p_data_diff.add_argument(
        "--key", action="append", default=[], metavar="COL",
        help="key column giving each row its identity; repeatable. Without one "
             "the diff falls back to a content set-diff and cannot report "
             "MODIFIED rows.")
    p_data_diff.add_argument("--json", action="store_true", dest="as_json")

    p_status = sub.add_parser("status", help="op-log status")
    p_status.add_argument("--json", action="store_true", dest="as_json")
    p_graph = sub.add_parser(
        "graph", help="render the op-log as a graph (mermaid or json)")
    p_ev = sub.add_parser(
        "evidence", help="the measurable claim, computed from your own op-log")
    p_ev.add_argument("--json", action="store_true")
    p_ev.add_argument("--since", default=None, help="ISO timestamp")
    p_graph.add_argument("--format", choices=("mermaid", "json"),
                         default="mermaid")
    p_graph.add_argument("--since", default=None,
                         help="ISO timestamp — only ops at or after it")
    p_graph.add_argument("--actor", default=None, help="restrict to one actor")
    p_graph.add_argument("--out", default=None,
                         help="write to a file instead of stdout")
    p_graph.add_argument("--json", action="store_true", dest="as_json",
                         help="alias for --format json (the uniform read contract)")

    p_mp = sub.add_parser("merge-preview", help="node-level merge preview of two shas")
    p_mp.add_argument("a", help="base-side sha")
    p_mp.add_argument("b", help="merge-side sha")
    p_mp.add_argument("--actor", default=None)
    p_mp.add_argument("--json", action="store_true", dest="as_json")

    p_mc = sub.add_parser("merge-conflicts", help="list escalated merge conflicts")
    p_mc.add_argument("--json", action="store_true", dest="as_json")

    p_rc = sub.add_parser("resolve-conflict", help="mark a conflict resolved (human)")
    p_rc.add_argument("conflict_id")
    p_rc.add_argument("--body", default=None, help="file with the resolved node body")
    p_rc.add_argument("--resolver", default=None)

    p_lease = sub.add_parser("lease", help="lease registry operations")
    lsub = p_lease.add_subparsers(dest="lease_cmd", required=True)
    p_la = lsub.add_parser("acquire", help="acquire leases for targets")
    p_la.add_argument("targets", nargs="*")
    p_la.add_argument("--staged", action="store_true",
                      help="also lease every STAGED file the gate guards "
                           "(the one-command way to satisfy the pre-commit gate)")
    p_la.add_argument("--adopt", action="store_true",
                      help="with --staged: proceed even though some staged files "
                           "were not named. READ THE LIST FIRST — in a shared "
                           "worktree they are routinely a peer's work")
    p_la.add_argument("--ttl", type=int, default=300)
    p_la.add_argument("--reason", default="")
    p_la.add_argument("--actor", default=None)
    p_lh = lsub.add_parser("heartbeat", help="refresh leases")
    p_lh.add_argument("ids", nargs="+")
    p_lh.add_argument("--actor", default=None)
    p_lr = lsub.add_parser("release", help="release leases")
    p_lr.add_argument("ids", nargs="+")
    p_lr.add_argument("--actor", default=None)
    p_ll = lsub.add_parser("list", help="list active leases")
    p_ll.add_argument("--json", action="store_true", dest="as_json")
    lsub.add_parser("sweep", help="sweep expired leases")
    p_lc = lsub.add_parser(
        "contact",
        help="ask whoever holds a path's lease to release it -- the missing "
             "half of 'talk to them or wait'")
    p_lc.add_argument("path")
    p_lc.add_argument("-m", "--message", default="", help="why you need it")
    p_lc.add_argument("--actor", default=None)
    p_lq = lsub.add_parser(
        "requests",
        help="who is blocked on YOUR leases, and what you are waiting on")
    p_lq.add_argument("--actor", default=None)

    p_lc = sub.add_parser("lease-check", help="pre-commit lease gate")
    p_lc.add_argument("--actor", default=None)

    p_sm = sub.add_parser(
        "stage-mine",
        help="stage ONLY your edits to files other sessions are also editing",
    )
    # nargs="*" so `--self-test` needs no dummy path — a gate you cannot run
    # without inventing an argument is a gate that does not get run.
    p_sm.add_argument("paths", nargs="*", help="repo-relative paths you hold a lease on")
    p_sm.add_argument("--actor", default=None)
    p_sm.add_argument("--dry-run", action="store_true",
                      help="report what would be staged without touching the index")
    p_sm.add_argument("--require", action="append", default=[], metavar="TEXT",
                      help="text that MUST appear in the staged copy; repeatable. "
                           "Use it for the line that wires your change up — that is "
                           "the one a heuristic drops.")
    p_sm.add_argument("--self-test", action="store_true",
                      help="prove the merge and the completeness assertion still work")

    p_bodies = sub.add_parser(
        "bodies", help="content-addressed body store (read a sha / stats)"
    )
    p_bodies.add_argument("--get", default=None, help="content address (sha) to read")
    p_bodies.add_argument("--out", default=None, help="write the body to this file")
    p_bodies.add_argument("--json", action="store_true", dest="as_json")

    p_dedupe = sub.add_parser(
        "dedupe", help="body store stats / gc / disk dedupe scan for duplicate files"
    )
    p_dedupe.add_argument(
        "--scan", nargs="*", default=None, help="trees to hash for duplicate files"
    )
    p_dedupe.add_argument(
        "--reclaim", nargs="*", default=None,
        help="hard-link duplicate files under these trees (dry-run unless --apply)",
    )
    p_dedupe.add_argument(
        "--gc", action="store_true",
        help="remove body blobs the op-log does not reference (dry-run unless --apply)",
    )
    p_dedupe.add_argument(
        "--apply", action="store_true",
        help="with --reclaim/--gc: actually act (never touches git-tracked / referenced)",
    )

    p_ledger = sub.add_parser(
        "ledger", help="op-log as attribution records (who changed what)"
    )
    p_ledger.add_argument(
        "--op", default=None, help="op_id OR ledger_ref to show (either is accepted)"
    )
    p_ledger.add_argument("--sha", default=None, help="git sha to show")
    p_ledger.add_argument(
        "--json", action="store_true",
        help="emit full ops as JSON (machine surface; the text form is lossy)",
    )

    p_sync = sub.add_parser(
        "sync", help="differential sync over the mesh (ops + bodies)"
    )
    p_sync.add_argument(
        "--export", action="store_true", help="export the delta to a bundle file"
    )
    p_sync.add_argument("--out", default=None, help="bundle path for --export")
    p_sync.add_argument(
        "--known", nargs="*", default=None,
        help="op_ids the peer already has (default: none = full clone)",
    )
    p_sync.add_argument(
        "--meta-only", action="store_true",
        help="export ops WITHOUT bodies (peers pull bodies on-demand by sha)",
    )
    p_sync.add_argument(
        "--import", dest="import_file", default=None, help="bundle file to import"
    )
    p_sync.add_argument(
        "--data-root", default=None, help="vcs store directory (default: Library/Data/vcs)"
    )
    # The VERB forms the README and both skills have always documented
    # (`awgit sync export -o delta.json`). They were never implemented: the
    # only real spelling was `sync --export`, so every documented sync line
    # died with "unrecognized arguments: export". The flags stay for anyone
    # who scripted against them; these are what the docs promise.
    ssub = p_sync.add_subparsers(dest="sync_cmd", required=False)
    # default=SUPPRESS on every option the PARENT also defines. Without it a
    # subparser's default OVERWRITES what the parent already parsed, silently:
    # `sync --meta-only export -o x.json` came back with meta_only=False and
    # exported the bodies the user asked to omit. No error, just the wrong
    # thing. Asserted by check_awgit_cli_contract ACC004.
    keep = argparse.SUPPRESS  # do not clobber the parent's value
    p_sx = ssub.add_parser("export", help="export the delta a peer is missing")
    p_sx.add_argument("--out", "-o", default=keep, help="bundle path")
    p_sx.add_argument("--known", nargs="*", default=keep,
                      help="op_ids the peer already has (default: none = full clone)")
    p_sx.add_argument("--meta-only", action="store_true", default=keep,
                      help="ops WITHOUT bodies (peers pull bodies on demand)")
    p_sx.add_argument("--data-root", default=keep)
    p_si = ssub.add_parser("import", help="apply a bundle (idempotent)")
    p_si.add_argument("bundle", help="bundle file to import")
    p_si.add_argument("--data-root", default=keep)

    # git passthrough. Registered FROM the allowlist so the CLI and awgit.git
    # cannot disagree about which verbs are forwarded.
    for verb in PASSTHROUGH_VERBS:
        p_fwd = sub.add_parser(verb, help=f"git {verb} (forwarded unchanged)")
        p_fwd.add_argument("rest", nargs=argparse.REMAINDER)
    p_git = sub.add_parser("git", help="run any git command: awgit git -- <args>")
    p_git.add_argument("rest", nargs=argparse.REMAINDER)
    p_commit = sub.add_parser(
        "commit", help="git commit, lease-checked and captured")
    p_commit.add_argument("rest", nargs=argparse.REMAINDER)
    p_commit.add_argument("--actor", default=None)

    p_clone = sub.add_parser(
        "clone", help="clone lazily — history now, file contents on demand")
    p_clone.add_argument("url")
    p_clone.add_argument("dest")
    p_clone.add_argument("--paths", nargs="*", default=None,
                         help="limit the working tree to these directories")
    p_clone.add_argument("--json", action="store_true", dest="as_json")

    p_ckt = sub.add_parser("sparse", help="the sparse working tree")
    cksub = p_ckt.add_subparsers(dest="sparse_cmd", required=True)
    ck_add = cksub.add_parser("add", help="materialise more directories")
    ck_add.add_argument("paths", nargs="+")
    ck_st = cksub.add_parser("status", help="is this clone actually lazy?")
    ck_st.add_argument("--json", action="store_true", dest="as_json")

    p_owners = sub.add_parser(
        "owners", help="who owns this — declared AND measured from history")
    p_owners.add_argument("path", nargs="?", default="")
    p_owners.add_argument("--json", action="store_true", dest="as_json")

    p_code = sub.add_parser("code", help="where a symbol is defined")
    cdsub = p_code.add_subparsers(dest="code_cmd", required=True)
    for _v, _h in (("def", "exact name"), ("search", "substring")):
        _p = cdsub.add_parser(_v, help=f"find by {_h}")
        _p.add_argument("symbol")
        _p.add_argument("--json", action="store_true", dest="as_json")

    p_prove = sub.add_parser(
        "prove", help="evidence for this change: nodes touched + gate results")
    p_prove.add_argument("sha", nargs="?", default="HEAD")
    p_prove.add_argument("--paths", nargs="*", default=None)
    p_prove.add_argument("--markdown", action="store_true", help="as a PR comment")
    p_prove.add_argument("--json", action="store_true", dest="as_json")

    p_queue = sub.add_parser(
        "queue", help="GitHub's merge queue (enqueue/status)")
    qsub = p_queue.add_subparsers(dest="queue_cmd", required=True)
    q_add = qsub.add_parser("enqueue", help="add a PR to the merge queue")
    q_add.add_argument("number", type=int)
    qsub.add_parser("status", help="what is queued")

    p_ci = sub.add_parser("ci", help="CI runs for this branch")
    cisub = p_ci.add_subparsers(dest="ci_cmd", required=True)
    for _v in ("status", "logs"):
        cisub.add_parser(_v, help=f"gh run {_v}")

    p_review = sub.add_parser(
        "review", help="review threads, anchored to nodes so they survive rebases")
    rvsub = p_review.add_subparsers(dest="review_cmd", required=True)
    p_rv_show = rvsub.add_parser("show", help="threads on this change")
    p_rv_show.add_argument("--change-id", dest="change_id", default="")
    p_rv_show.add_argument("--json", action="store_true", dest="as_json")
    p_rv_c = rvsub.add_parser("comment", help="draft a comment on a node")
    p_rv_c.add_argument("--node", required=True, help="node id to anchor to")
    p_rv_c.add_argument("-b", "--body", required=True)
    p_rv_c.add_argument("--file", default="", help="where the node is now (a hint)")
    p_rv_c.add_argument("--symbol", default="")
    p_rv_c.add_argument("--change-id", dest="change_id", default="")
    p_rv_c.add_argument("--actor", default=None)
    p_rv_s = rvsub.add_parser("submit", help="publish every draft")
    p_rv_s.add_argument("--change-id", dest="change_id", default="")
    p_rv_s.add_argument("--approve", action="store_true")
    p_rv_r = rvsub.add_parser("resolve", help="close a thread")
    p_rv_r.add_argument("thread_id")
    p_rv_r.add_argument("--change-id", dest="change_id", default="")

    p_push = sub.add_parser(
        "push", help="publish the stack — one pull request per commit")
    p_push.add_argument("--apply", action="store_true",
                        help="actually push and open/update PRs")
    p_push.add_argument("--trunk", default="")
    p_push.add_argument("--offline", action="store_true",
                        help="plan without asking GitHub which PRs exist")
    p_push.add_argument("--json", action="store_true", dest="as_json")

    p_pr = sub.add_parser("pr", help="pull requests (list/view/checks/merge)")
    prsub = p_pr.add_subparsers(dest="pr_cmd", required=True)
    for _verb in ("list", "view", "checks", "merge", "create"):
        _p = prsub.add_parser(_verb, help=f"gh pr {_verb}")
        _p.add_argument("rest", nargs=argparse.REMAINDER)
    p_wait = prsub.add_parser(
        "wait", help="block until a PR is merged/mergeable/checked (124 on timeout)")
    p_wait.add_argument("number", type=int)
    p_wait.add_argument("--for", dest="for_state", default="merged",
                        choices=("merged", "mergeable", "checks"))
    p_wait.add_argument("--timeout", type=int, default=1800)
    p_wait.add_argument("--interval", type=int, default=15)

    p_absorb = sub.add_parser(
        "absorb", help="fold pending changes into the commits that own them")
    p_absorb.add_argument("--apply", action="store_true",
                          help="actually rewrite (default: show the routing)")
    p_absorb.add_argument("--trunk", default="")
    p_absorb.add_argument("--paths", nargs="*", default=None,
                          help="only consider these pending files")
    p_absorb.add_argument("--all", action="store_true", dest="scan_all",
                          help="scan every pending file (slow on a large tree)")
    p_absorb.add_argument("--depth", type=int, default=None,
                          help="commits to search for owners "
                               "(default 30; truncation is reported)")
    p_absorb.add_argument("--json", action="store_true", dest="as_json")

    sub.add_parser("uncommit", help="undo the last commit, keep the changes")
    p_restack = sub.add_parser("restack", help="rebase the stack onto trunk")
    p_restack.add_argument("--trunk", default="")
    p_pull = sub.add_parser("pull", help="fetch, then restack onto trunk")
    p_pull.add_argument("--trunk", default="")

    p_stack = sub.add_parser(
        "stack", aliases=["sl"], help="your commits above trunk, one PR each")
    p_stack.add_argument("--trunk", default="", help="override trunk detection")
    p_stack.add_argument("--json", action="store_true", dest="as_json")
    for _move in ("prev", "next"):
        p_mv = sub.add_parser(
            _move, help=f"check out the {'older' if _move == 'prev' else 'newer'} "
                        f"commit in the stack")
        p_mv.add_argument("--trunk", default="")

    p_wt = sub.add_parser(
        "worktree", help="your own checkout — where rewrites are always safe")
    wtsub = p_wt.add_subparsers(dest="worktree_cmd", required=True)
    p_wt_n = wtsub.add_parser("new", help="create a worktree and branch")
    p_wt_n.add_argument("name")
    p_wt_n.add_argument("--at", default="HEAD", help="commit to branch from")
    p_wt_l = wtsub.add_parser("list", help="worktrees git knows about")
    p_wt_l.add_argument("--json", action="store_true", dest="as_json")
    p_wt_r = wtsub.add_parser("rm", help="remove a worktree")
    p_wt_r.add_argument("name")
    p_wt_r.add_argument("--force", action="store_true",
                        help="remove even with uncommitted changes (can discard work)")

    def _h_scratch(a):
        from awgit.scratch import cmd_scratch
        return cmd_scratch(a.dest, a.branch, a.url)

    def _h_blob_commit(a):
        from awgit.scratch import cmd_blob_commit, selftest
        if a.selftest:
            return selftest()
        if not (a.base and a.message and a.paths):
            print("vcs: blob-commit needs --base, -m and at least one path")
            return 2
        return cmd_blob_commit(a.base, a.branch, a.message, a.paths,
                               push=a.push, allow_shrink=a.allow_shrink,
                               src=Path(a.src_dir) if a.src_dir else None,
                               advance=a.advance,
                               allow_stale=a.allow_stale)

    # 🚨 cmd_reconcile_index existed with NO subcommand, and it PRINTS
    # "pass --apply to make the index agree with HEAD for the phantom paths
    # only" -- advice for a flag on a command the CLI did not expose, so it
    # could not be followed. The class: an advertised command that is not a
    # declared entry point -- documentation for something a user cannot run.
    #
    # A phantom staged deletion is common in THIS tree specifically: branches
    # move under a shared index every few minutes, and the residue reads as
    # "someone deleted this file" in every status a peer runs.
    def _h_reconcile_index(a):
        from awgit.scratch import cmd_reconcile_index
        return cmd_reconcile_index(apply=a.apply)

    p_ri = sub.add_parser(
        "reconcile-index",
        help="find (and with --apply, repair) PHANTOM staged deletions — a "
             "path the index calls deleted while the file is present and "
             "byte-identical to HEAD")
    p_ri.add_argument("--apply", action="store_true",
                      help="make the index agree with HEAD for the phantom "
                           "paths only (default: report, change nothing — a "
                           "real `git rm --cached` is indistinguishable from "
                           "the outside, so repair is opt-in)")
    p_ri.set_defaults(_awgit_handler=_h_reconcile_index)

    p_scr = sub.add_parser(
        "scratch",
        help="a partial clone of origin with your identity — merge surgery "
             "without touching the shared tree")
    p_scr.add_argument("dest")
    p_scr.add_argument("--branch", default="", help="default: origin HEAD")
    p_scr.add_argument("--url", default="", help="default: this repo's origin")
    p_scr.set_defaults(_awgit_handler=_h_scratch)

    p_bc = sub.add_parser(
        "blob-commit",
        help="commit exactly these worktree files onto a base ref via a "
             "private index — the shared index and worktree are never touched")
    p_bc.add_argument("paths", nargs="*", help="worktree paths (absent = record deletion)")
    p_bc.add_argument("--base", default="", help="ref to commit on top of")
    p_bc.add_argument("--branch", default="", help="branch name for the push hint")
    p_bc.add_argument("-m", "--message", default="")
    p_bc.add_argument("--push", action="store_true",
                      help="push the commit to refs/heads/<branch>")
    p_bc.add_argument("--allow-shrink", action="store_true", dest="allow_shrink",
                      help="permit a named file to shrink sharply vs the base "
                           "(default: refused — a stale copy sweeps peers)")
    p_bc.add_argument("--from", default="", dest="src_dir", metavar="DIR",
                      help="read the files from this staging dir instead of "
                           "the shared worktree (same relative paths)")
    p_bc.add_argument("--allow-stale", action="store_true", dest="allow_stale",
                      help="commit a --from copy older than the base's newest "
                           "commit for it (default: refused - it would revert)")
    # 🚨 The --advance PATH EXISTED AND WAS UNREACHABLE. `advance` is a
    # parameter of both cmd_blob_commit and _blob_commit, threaded through and
    # implemented with care -- a strict fast-forward that REFUSES when a peer
    # has moved the ref -- and no argparse flag ever set it, so it was always
    # False. The class exactly: an opt-in with no opt-in control is deleted
    # code, however carefully the thing behind it was written.
    #
    # What that cost: blob-commit builds its commit with commit-tree, which
    # references it from NOTHING and writes NO reflog entry. `git log <branch>`
    # cannot see it and `git branch -a --contains` comes back empty, so the work
    # looks committed and is one gc from gone. Measured 2026-08-24: eight such
    # commits in a single session, one of them a whole checker, noticed by
    # accident.
    p_bc.add_argument("--advance", action="store_true",
                      help="fast-forward refs/heads/<branch> to the new commit "
                           "when it is still at --base (refused if a peer moved "
                           "it; your commit is safe by sha either way). Without "
                           "this the commit is on NO branch and no reflog.")
    p_bc.add_argument("--selftest", action="store_true",
                      help="prove isolation: a peer's staged edit must not leak")
    p_bc.set_defaults(_awgit_handler=_h_blob_commit)

    def _h_fresh(a):
        from awgit.scratch import cmd_fresh
        return cmd_fresh(a.ref, a.paths)

    def _h_union_rows(a):
        from awgit.scratch import cmd_union_rows
        return cmd_union_rows(a.path, key_pattern=a.key_pattern, ref=a.ref)

    p_fr = sub.add_parser(
        "fresh",
        help="is my copy BEHIND a ref? run before editing a file peers move — "
             "the pre-edit half of the blob-commit sweep guard")
    p_fr.add_argument("ref")
    p_fr.add_argument("paths", nargs="+")
    p_fr.set_defaults(_awgit_handler=_h_fresh)

    p_ur = sub.add_parser(
        "union-rows",
        help="resolve a conflicted append-only row ledger by id-keyed union — "
             "every id kept once, nothing dropped")
    p_ur.add_argument("path")
    p_ur.add_argument("--key-pattern", default="", dest="key_pattern",
                      help=r"regex whose group(1) is the row id "
                           r"(default: markdown '| XX-123 |' rows)")
    p_ur.add_argument("--ref", default="",
                      help="union against a REF instead of a merge conflict — "
                           "for two lineages that diverged without ever "
                           "conflicting. Local is always the spine, so your "
                           "unpushed rows are never dropped.")
    p_ur.set_defaults(_awgit_handler=_h_union_rows)

    def _h_read(a):
        from awgit.scratch import cmd_read
        return cmd_read(a.ref, a.path, out=a.out)

    p_rd = sub.add_parser(
        "read",
        help="read a path at another ref — refuses rather than returning the "
             "silence MSYS path-mangling turns into a false 'absent'")
    p_rd.add_argument("ref")
    p_rd.add_argument("path")
    p_rd.add_argument("--out", default="",
                      help="write bytes to this file instead of stdout")
    p_rd.set_defaults(_awgit_handler=_h_read)

    def _h_port(a):
        from awgit.scratch import cmd_port
        return cmd_port(a.shas, a.onto, message=a.message, branch=a.branch,
                        push=a.push, paths=a.paths or None,
                        overwrite_diverged=a.overwrite_diverged)

    p_pt = sub.add_parser(
        "port",
        help="port commits' file content onto another lineage — no worktree, "
             "no conflict, and a REFUSAL where the base moved under a path")
    p_pt.add_argument("shas", nargs="+")
    p_pt.add_argument("--onto", required=True, help="base ref to commit onto")
    p_pt.add_argument("-m", "--message", default="")
    p_pt.add_argument("--branch", default="")
    p_pt.add_argument("--push", action="store_true")
    p_pt.add_argument("--paths", nargs="*", default=[],
                      help="subset of the touched paths to carry")
    p_pt.add_argument("--overwrite-diverged", action="store_true",
                      dest="overwrite_diverged",
                      help="carry a path the base also changed (read what the "
                           "base did FIRST — it often already fixed it)")
    p_pt.set_defaults(_awgit_handler=_h_port)

    def _h_ship(a):
        from awgit.scratch import cmd_ship
        body = a.body
        if a.body_file:
            body = Path(a.body_file).read_text(encoding="utf-8")
        if not (a.base and a.branch):
            print("vcs: ship needs --base and --branch")
            return 2
        if a.paths and not a.message:
            print("vcs: ship needs -m when committing paths")
            return 2
        return cmd_ship(a.base, a.branch, a.message, a.paths, title=a.title,
                        body=body, merge=a.merge,
                        delete_branch=a.delete_branch,
                        allow_shrink=a.allow_shrink,
                        src=Path(a.src_dir) if a.src_dir else None,
                        allow_stale=a.allow_stale)

    p_sh = sub.add_parser(
        "ship",
        help="commit -> push -> PR -> (optionally) merge, keeping every guard: "
             "private index, shrink refusal, and a merge VERIFIED by reading "
             "the PR state back instead of trusting gh's exit code")
    p_sh.add_argument("paths", nargs="*")
    p_sh.add_argument("--base", default="", help="ref to commit onto")
    p_sh.add_argument("--branch", default="", help="branch to push")
    p_sh.add_argument("-m", "--message", default="")
    p_sh.add_argument("--title", default="", help="PR title (default: first line of -m)")
    p_sh.add_argument("--body", default="")
    p_sh.add_argument("--body-file", default="", dest="body_file")
    p_sh.add_argument("--merge", action="store_true", help="land it")
    p_sh.add_argument("--delete-branch", action="store_true",
                      dest="delete_branch",
                      help="delete the remote branch after a VERIFIED merge "
                           "(via the API — gh's own --delete-branch runs a "
                           "local checkout that fails with live worktrees)")
    p_sh.add_argument("--allow-shrink", action="store_true", dest="allow_shrink")
    p_sh.add_argument("--allow-stale", action="store_true", dest="allow_stale",
                      help="commit a --from copy older than the base's newest "
                           "commit for it (default: refused - it would revert)")
    p_sh.add_argument("--from", default="", dest="src_dir", metavar="DIR",
                      help="read the files from this staging dir instead of "
                           "the shared worktree — lets you prepare a change in "
                           "a clean copy and ship it without writing into a "
                           "tree other sessions are committing to")
    p_sh.set_defaults(_awgit_handler=_h_ship)

    p_cid = sub.add_parser(
        "change-id", help="the stable id that survives amend/rebase/cherry-pick"
    )
    cidsub = p_cid.add_subparsers(dest="changeid_cmd", required=True)
    p_cid_e = cidsub.add_parser("ensure", help="stamp a message file (the git hook)")
    p_cid_e.add_argument("file", help="commit message file")
    p_cid_e.add_argument("--source", default="", help="git prepare-commit-msg source")
    p_cid_s = cidsub.add_parser("show", help="the change-id of a commit")
    p_cid_s.add_argument("rev", nargs="?", default="HEAD")
    p_cid_s.add_argument("--json", action="store_true", dest="as_json")
    p_cid_f = cidsub.add_parser("find", help="commits carrying a change-id")
    p_cid_f.add_argument("change_id")
    p_cid_f.add_argument("--json", action="store_true", dest="as_json")

    p_cmds = sub.add_parser(
        "commands", help="describe the whole CLI (use --json; never scrape --help)"
    )
    p_cmds.add_argument("--json", action="store_true", dest="as_json")

    p_version = sub.add_parser("version", help="awgit version")
    p_version.add_argument("--json", action="store_true", dest="as_json")

    p_init = sub.add_parser(
        "init", help="set awgit up here: verify the actor, install capture hooks"
    )
    p_init.add_argument("--actor", default=None)
    p_init.add_argument("--repo", default=None, help="repo root (default: cwd)")
    p_init.add_argument("--no-hooks", action="store_true",
                        help="report identity and store, install nothing")

    p_hooks = sub.add_parser(
        "hooks", help="install/uninstall the chained capture hooks"
    )
    p_hooks.add_argument("action", choices=["install", "uninstall"])
    p_hooks.add_argument(
        "--repo", default=None, help="repo root (default: current directory)"
    )

    # Host extension seam. An embedding application (the AitherOS monorepo's
    # overlay, for one) adds its own subcommands here instead of forking this
    # module — an extension sets ``_awgit_handler`` via ``set_defaults`` and is
    # dispatched below. See awgit/plugins.py for why this is a seam.
    from awgit import plugins

    plugins.extend_parser(sub)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    # GENERATED doctor intercept (gen_aw_doctor.py) -- do not edit
    _dv = locals().get("argv")
    if (_dv if _dv is not None else __import__("sys").argv[1:])[:1] == ["doctor"]:
        from ._doctor import report
        return report()
    argv = list(sys.argv[1:] if argv is None else argv)

    # Passthrough is intercepted BEFORE argparse, not dispatched through it.
    # argparse.REMAINDER cannot carry `awgit log --oneline`: it still tries to
    # match a leading `--oneline` as an option of the subparser and errors with
    # awgit's own usage, which is a confusing way to say "git would have
    # understood that". The verbs are still REGISTERED as subparsers so
    # `awgit commands` can describe them; this only decides who parses.
    if argv and argv[0] in PASSTHROUGH_VERBS:
        from awgit.git import forward, strip_separator

        return forward(argv[0], strip_separator(argv[1:]))
    if argv and argv[0] == "git":
        from awgit.git import run, strip_separator

        rest = strip_separator(argv[1:])
        if not rest:
            print("awgit: usage: awgit git [--] <git args>", file=sys.stderr)
            return 2
        return run(rest)
    if argv and argv[0] == "commit":
        # Pull awgit's OWN flags out before anything reaches git. Forwarding
        # them raw made `awgit commit --actor x -m y` die with git's
        # "unknown option `actor'" -- a flag awgit advertises, rejected by a
        # tool the user did not think they were talking to.
        rest, actor = list(argv[1:]), None
        if "--actor" in rest:
            i = rest.index("--actor")
            if i + 1 < len(rest):
                actor = rest[i + 1]
                del rest[i:i + 2]
            else:
                print("awgit: --actor needs a value", file=sys.stderr)
                return 2
        return _cmd_commit(argparse.Namespace(rest=rest, actor=actor))

    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "_awgit_handler", None)
    if handler is not None:
        return int(handler(args))
    if args.cmd == "capture":
        return _cmd_capture(args)
    if args.cmd == "diff":
        return _cmd_diff(args)
    if args.cmd == "data":
        return _cmd_data(args)
    if args.cmd == "status":
        return _cmd_status(args)
    if args.cmd == "merge-preview":
        return _cmd_merge_preview(args)
    if args.cmd == "merge-conflicts":
        return _cmd_merge_conflicts(args)
    if args.cmd == "resolve-conflict":
        return _cmd_resolve_conflict(args)
    if args.cmd == "lease":
        return _cmd_lease(args)
    if args.cmd == "lease-check":
        return _cmd_lease_check(args)
    if args.cmd == "stage-mine":
        return _cmd_stage_mine(args)
    if args.cmd == "graph":
        return _cmd_graph(args)
    if args.cmd == "evidence":
        return _cmd_evidence(args)
    if args.cmd == "bodies":
        return _cmd_bodies(args)
    if args.cmd == "dedupe":
        return _cmd_dedupe(args)
    if args.cmd == "ledger":
        return _cmd_ledger(args)
    if args.cmd == "sync":
        return _cmd_sync(args)
    if args.cmd == "commit":
        return _cmd_commit(args)
    if args.cmd == "git" or args.cmd in PASSTHROUGH_VERBS:
        return _cmd_git_passthrough(args)
    if args.cmd == "clone":
        return _cmd_clone(args)
    if args.cmd == "sparse":
        return _cmd_sparse(args)
    if args.cmd == "owners":
        return _cmd_owners(args)
    if args.cmd == "code":
        return _cmd_code(args)
    if args.cmd == "prove":
        return _cmd_prove(args)
    if args.cmd == "queue":
        if args.queue_cmd == "enqueue":
            # GitHub's native merge queue, not one of ours: it already rebases,
            # re-tests and lands, and a second implementation would be a
            # quarters-long service that disagrees with it.
            return subprocess.run(["gh", "pr", "merge", str(args.number),
                                   "--auto", "--squash"]).returncode
        return subprocess.run(["gh", "pr", "list", "--state", "open",
                               "--json", "number,title,mergeStateStatus"]).returncode
    if args.cmd == "ci":
        verb = ["run", "list"] if args.ci_cmd == "status" else ["run", "view", "--log"]
        return subprocess.run(["gh", *verb]).returncode
    if args.cmd == "review":
        return _cmd_review(args)
    if args.cmd == "push":
        return _cmd_push(args)
    if args.cmd == "pr":
        return _cmd_pr(args)
    if args.cmd == "absorb":
        return _cmd_absorb(args)
    if args.cmd == "uncommit":
        return _cmd_uncommit(args)
    if args.cmd in ("restack", "pull"):
        return _cmd_restack(args)
    if args.cmd == "worktree":
        return _cmd_worktree(args)
    if args.cmd in ("stack", "sl"):
        return _cmd_stack(args)
    if args.cmd in ("prev", "next"):
        return _cmd_move(args)
    if args.cmd == "change-id":
        return _cmd_change_id(args)
    if args.cmd == "commands":
        return _cmd_commands(args)
    if args.cmd == "version":
        return _cmd_version(args)
    if args.cmd == "init":
        return _cmd_init(args)
    if args.cmd == "hooks":
        return _cmd_hooks(args)
    parser.error(f"unknown command {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
