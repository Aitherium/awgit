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
    return (
        getattr(args, "actor", None)
        or os.environ.get("AITHER_ACTOR")
        or "unknown"
    )


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
        try:
            leases = registry.acquire(
                who, args.targets, ttl_sec=args.ttl, reason=args.reason
            )
        except LeaseConflictError as exc:
            print(f"vcs: {exc}", file=sys.stderr)
            return 1
        for lz in leases:
            print(f"vcs: lease {lz.lease_id} {lz.target} until {lz.expires_ts}")
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
    from awgit.ledger import op_to_ledger_entry
    from awgit.oplog import OpLog

    ops = OpLog().all_ops()
    if args.op:
        ops = [o for o in ops if o.op_id == args.op]
    elif args.sha:
        ops = [o for o in ops if o.git_sha == args.sha]
    if not ops:
        print("vcs: ledger: no ops match", file=sys.stderr)
        return 1
    for op in ops:
        entry = op_to_ledger_entry(op)
        verified = (
            f" (verified {entry.verified_actor})" if entry.actor_verified else ""
        )
        print(
            f"{entry.ledger_ref} {entry.actor}{verified} "
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="awgit", description="AitherOS semantic VCS")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_capture = sub.add_parser("capture", help="capture a commit as an EditOp")
    p_capture.add_argument("--sha", required=True)
    p_capture.add_argument("--actor", default=None)
    p_capture.add_argument(
        "--data-root",
        default=None,
        help="vcs store directory (default: AitherOS Library/Data/vcs)",
    )

    p_diff = sub.add_parser("diff", help="node-level diff between two shas")
    p_diff.add_argument("a", help="base sha")
    p_diff.add_argument("b", help="target sha")

    sub.add_parser("status", help="op-log status")

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
    p_la.add_argument("targets", nargs="+")
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
    p_ledger.add_argument("--op", default=None, help="op_id to show")
    p_ledger.add_argument("--sha", default=None, help="git sha to show")

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
