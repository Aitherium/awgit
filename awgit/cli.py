"""CLI entrypoint for the semantic-VCS layer (`python -m lib.awgit.cli`).

Used by git hooks (capture, lease-check) and by agents (status, diff, lease).
Runs as its own sync process — no event loop is active, so the blocking file
locks in the stores are PQ010-compliant.

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
from awgit.leases import LeaseConflictError, LeaseRegistry, coverage_gap
from awgit.merge import list_conflicts, merge_ops, resolve_conflict
from awgit.oplog import OpLog


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
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    try:
        changes = diff_git(args.a, args.b)
    except ValueError as exc:
        print(f"vcs: {exc}", file=sys.stderr)
        return 2
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
    print(f"vcs: merge status: {result.status}")
    for note in result.notes:
        print(f"  {note}")
    for c in result.conflicts:
        print(f"  CONFLICT {c.node_id} ({c.symbol}) — escalate for human review")
    return 0 if result.status != "conflict" else 1


def _cmd_merge_conflicts(args: argparse.Namespace) -> int:
    for c in list_conflicts():
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
    print(f"vcs: {len(ops)} ops in {log.path}")
    if last is not None:
        print(f"  last: {last.ts} {last.actor} {last.summary}")
    return 0


def _cmd_lease(args: argparse.Namespace) -> int:
    registry = LeaseRegistry()
    cmd = args.lease_cmd
    who = _actor(args)
    if cmd == "acquire":
        targets = list(args.targets or [])
        if getattr(args, "staged", False):
            # Lease exactly what the gate will check. Without this, widening
            # coverage beyond .py (D-1880) would have made a routine commit a
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
            # hold a lease on their work. Measured 2026-08-10 (D-1887): three
            # portal-kit files a peer had staged seconds earlier were adopted in
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
                      "pass on a commit that sweeps it (D-1887).", file=sys.stderr)
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
            print(f"vcs: {exc}", file=sys.stderr)
            return 1
        # A lease over an ALREADY-DIRTY file captures a baseline that contains work
        # which is not yours, and `stage-mine` computes (baseline -> worktree), so it
        # cannot separate what it never saw as separate. Leasing after a peer has
        # started editing therefore looks exactly like leasing a clean file, and the
        # commit sweeps them — measured 2026-08-10, ~29 lines of a peer's in-flight
        # HYG004 work landed in someone else's commit that way.
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
    if cmd == "heartbeat":
        print(f"vcs: heartbeat refreshed {registry.heartbeat(who, args.ids)} leases")
        return 0
    if cmd == "release":
        print(f"vcs: released {registry.release(who, args.ids)} leases")
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


def _cmd_lease_check(args: argparse.Namespace) -> int:
    if os.environ.get("VCS_LEASES_ENFORCE", "0") != "1":
        print("vcs: lease-check not enforced (VCS_LEASES_ENFORCE=0)")
        return 0
    repo = Path(os.environ.get("VCS_REPO_ROOT", os.getcwd()))
    who = _actor(args)
    if who == "unknown":
        print("vcs: lease-check requires AITHER_ACTOR (or --actor)", file=sys.stderr)
        return 1
    gap = coverage_gap(_staged_files(repo), who)
    if gap:
        print(
            "vcs: commit rejected — no active lease covering: " + ", ".join(gap),
            file=sys.stderr,
        )
        return 1
    print("vcs: lease-check OK")
    return 0



def _cmd_graph(args: argparse.Namespace) -> int:
    from awgit.graph import build, to_json, to_mermaid

    g = build(since=args.since, actor=args.actor)
    text = to_json(g) if args.format == "json" else to_mermaid(g)
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


def main(argv: Optional[List[str]] = None) -> int:
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

    p_diff = sub.add_parser("diff", help="node-level diff between two shas")
    p_diff.add_argument("a", help="base sha")
    p_diff.add_argument("b", help="target sha")

    sub.add_parser("status", help="op-log status")
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

    p_mp = sub.add_parser("merge-preview", help="node-level merge preview of two shas")
    p_mp.add_argument("a", help="base-side sha")
    p_mp.add_argument("b", help="merge-side sha")
    p_mp.add_argument("--actor", default=None)

    sub.add_parser("merge-conflicts", help="list escalated merge conflicts")

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
                           "worktree they are routinely a peer's work (D-1887)")
    p_la.add_argument("--ttl", type=int, default=300)
    p_la.add_argument("--reason", default="")
    p_la.add_argument("--actor", default=None)
    p_lh = lsub.add_parser("heartbeat", help="refresh leases")
    p_lh.add_argument("ids", nargs="+")
    p_lh.add_argument("--actor", default=None)
    p_lr = lsub.add_parser("release", help="release leases")
    p_lr.add_argument("ids", nargs="+")
    p_lr.add_argument("--actor", default=None)
    lsub.add_parser("list", help="list active leases")
    lsub.add_parser("sweep", help="sweep expired leases")

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

    p_hooks = sub.add_parser(
        "hooks", help="install/uninstall the chained capture hooks"
    )
    p_hooks.add_argument("action", choices=["install", "uninstall"])
    p_hooks.add_argument(
        "--repo", default=None, help="repo root (default: current directory)"
    )

    args = parser.parse_args(argv)
    if args.cmd == "capture":
        return _cmd_capture(args)
    if args.cmd == "diff":
        return _cmd_diff(args)
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
    if args.cmd == "hooks":
        return _cmd_hooks(args)
    parser.error(f"unknown command {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
