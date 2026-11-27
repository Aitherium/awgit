"""``awgit maintenance``: clear a repo's leftovers and (optionally) consolidate its packs.

DEFAULT IS A DRY RUN. It prints the plan -- pack count and bytes, tmp garbage with
sizes and ages, gc.pid state, what a repack would do -- and changes nothing.

``--apply`` takes ``<git-dir>/awgit-cache.lock`` (the same lock ``awgit caches
--fix`` and the guard use, so there is one writer of the derived caches at a time),
then removes only what ``cache_integrity`` says qualifies:

* ``tmp_pack_*`` / ``tmp_idx_*`` / ``tmp_rev_*`` older than 6 h and not held open
  by any process (24 h when open handles cannot be judged);
* a ``gc.pid`` older than 12 h, or whose pid is dead on this host.

``--apply --repack`` additionally runs, niced, ``git repack --geometric=2 -d
--write-midx``, then ``git multi-pack-index verify`` and the quick cache check.
Geometric repack rewrites only the small packs and keeps the big ones, so it is
the cheap way down from 1,000+ packs. It is refused while another git writer
(gc / repack / multi-pack-index / commit-graph write) is running.

Both refuse while the HOST's ``wsl-maintenance.lock`` is fresh. That lock belongs
to the host (a disk move or VM restart in progress), so like ``fleet_verbs`` it is
NOT relocated by ``AITHER_HOME``; ``AITHER_MAINT_LOCK`` overrides it for tests.

Nothing here schedules itself. Run it by hand, or from a job someone chose to add.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from awgit import cache_integrity as ci

MAINT_LOCK_MAX_AGE_S = 6 * 3600
MAINT_LOCK_ENV = "AITHER_MAINT_LOCK"
#: argv fragments of other git processes that write the same caches / packs
_WRITER_VERBS = (("gc",), ("repack",), ("multi-pack-index", "write"), ("multi-pack-index", "expire"),
                 ("multi-pack-index", "repack"), ("commit-graph", "write"), ("prune",), ("pack-refs",))


def maint_lock_path() -> Path:
    raw = os.environ.get(MAINT_LOCK_ENV, "").strip()
    return Path(raw) if raw else Path.home() / ".aither" / "wsl-maintenance.lock"


def host_maintenance_fresh(now: Optional[float] = None) -> Optional[str]:
    """A message when the host's maintenance lock is fresh (< 6 h), else None."""
    p = maint_lock_path()
    try:
        age = (time.time() if now is None else now) - p.stat().st_mtime
    except OSError:
        return None
    if age < MAINT_LOCK_MAX_AGE_S:
        return f"{p} is fresh ({age / 60:.0f} min old): the host is under maintenance"
    return None


def writers_running(proc: str = "/proc") -> List[str]:
    """Command lines of other git processes that write packs or the derived caches."""
    out: List[str] = []
    try:
        pids = [d for d in os.listdir(proc) if d.isdigit() and int(d) != os.getpid()]
    except OSError:
        return out
    for pid in pids:
        try:
            raw = Path(proc, pid, "cmdline").read_bytes()
        except OSError:
            continue
        argv = [a for a in raw.decode("utf-8", "replace").split("\0") if a]
        if not argv or Path(argv[0]).name != "git":
            continue
        # skip git's global options (-c k=v, -C dir, --git-dir=...) to find the verb
        i, rest = 1, []
        while i < len(argv):
            a = argv[i]
            if a in ("-c", "-C", "--git-dir", "--work-tree"):
                i += 2
                continue
            if a.startswith("-"):
                i += 1
                continue
            rest = argv[i:]
            break
        if any(tuple(rest[:len(v)]) == v for v in _WRITER_VERBS):
            out.append(f"pid {pid}: {' '.join(argv)[:160]}")
    return out


def _pack_bytes(git_dir: Path) -> Optional[int]:
    try:
        return sum(e.stat().st_size for e in os.scandir(ci.pack_dir(git_dir)) if e.name.endswith(".pack"))
    except OSError:
        return None


def repack_argv(git_dir: Path) -> List[str]:
    base = ["git", "--git-dir", str(git_dir), "-c", "pack.threads=2",
            "repack", "--geometric=2", "-d", "--write-midx"]
    pre: List[str] = []
    if shutil.which("nice"):
        pre += ["nice", "-n", "19"]
    if shutil.which("ionice"):
        pre += ["ionice", "-c3"]
    return pre + base


def plan(git_dir: Path, *, repack: bool = False, now: Optional[float] = None) -> Dict[str, Any]:
    """Everything maintenance would do, as data. Read-only."""
    rep = ci.scan(git_dir, now=now)
    g = rep["garbage"]
    return {"git_dir": str(git_dir), "packs": rep["packs"]["count"], "pack_bytes": _pack_bytes(git_dir),
            "garbage": g, "gc_pid": rep["gc_pid"], "caches": rep["caches"],
            "bad_caches": [v["path"] for v in rep["caches"] if v["state"] == ci.BAD],
            "repack": {"requested": repack, "argv": repack_argv(git_dir)},
            "host_lock": host_maintenance_fresh(now), "writers": writers_running(),
            "warnings": rep["warnings"], "unjudged": rep["unjudged"]}


def apply(git_dir: Path, *, repack: bool = False, now: Optional[float] = None,
          run_cmd: Any = None) -> Dict[str, Any]:
    """Execute the plan under the cache lock. ``run_cmd(argv) -> returncode`` is a test seam."""
    res: Dict[str, Any] = {"ok": False, "refused": "", "removed": [], "freed_bytes": 0,
                           "gc_pid_removed": False, "healed": [], "repack": None, "errors": []}
    why = host_maintenance_fresh(now)
    if why:
        res["refused"] = why
        return res
    lk = ci.CacheLock(git_dir, owner="awgit maintenance")
    if not lk.acquire(now):
        res["refused"] = lk.why
        return res
    try:
        # judged AGAIN under the lock: the dry-run plan may be minutes old
        g = ci.tmp_garbage(git_dir, now=now)
        for f in g["files"]:
            if not f["qualifies"]:
                continue
            try:
                os.unlink(f["path"])
                res["removed"].append(f["path"])
                res["freed_bytes"] += f["size"]
            except FileNotFoundError:
                continue
            except OSError as exc:
                res["errors"].append(f"{f['path']}: {type(exc).__name__}: {exc}")
        gp = ci.gc_pid_state(git_dir, now=now)
        if gp.get("stale"):
            try:
                os.unlink(gp["path"])
                res["gc_pid_removed"] = True
            except OSError as exc:
                res["errors"].append(f"{gp['path']}: {type(exc).__name__}: {exc}")
        if repack:
            res["repack"] = _repack(git_dir, lk, res, run_cmd)
        if res["removed"] or res["gc_pid_removed"] or res["repack"]:
            ci.record({"action": "maintenance", "git_dir": str(git_dir), "removed": res["removed"],
                       "freed_bytes": res["freed_bytes"], "gc_pid_removed": res["gc_pid_removed"],
                       "repack": res["repack"]})
        res["ok"] = not res["errors"] and (res["repack"] is None or bool(res["repack"].get("ok")))
        return res
    finally:
        lk.release()


def _repack(git_dir: Path, lk: ci.CacheLock, res: Dict[str, Any], run_cmd: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "argv": repack_argv(git_dir)}
    busy = writers_running()
    if busy:
        out["refused"] = "another git writer is running: " + "; ".join(busy[:3])
        return out
    gp = ci.gc_pid_state(git_dir)
    if gp.get("present") and not gp.get("stale"):
        out["refused"] = f"gc.pid is live or recent ({gp['why']}): a gc may be running"
        return out
    # a bad MIDX would make repack read through it: set it aside first (same lock)
    healed = ci.heal(git_dir, lock=lk, trigger="awgit maintenance --repack")
    res["healed"] = healed["moved"]
    if not healed["ok"]:
        out["refused"] = "could not move a bad cache aside"
        return out
    runner = run_cmd or (lambda argv: subprocess.run(argv).returncode)
    out["packs_before"] = ci.pack_count(git_dir)
    t0 = time.time()
    out["repack_rc"] = runner(out["argv"])
    out["seconds"] = round(time.time() - t0)
    out["packs_after"] = ci.pack_count(git_dir)
    if out["repack_rc"] != 0:
        out["refused"] = f"repack exited {out['repack_rc']}; old packs are untouched until the new ones are written"
        return out
    out["verify_rc"] = runner(["git", "--git-dir", str(git_dir), "multi-pack-index", "verify"])
    out["quick"] = [v for v in ci.check_caches(git_dir) if v["state"] == ci.BAD]
    out["ok"] = out["verify_rc"] == 0 and not out["quick"]
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────

def add_parser(sub: Any) -> None:
    p = sub.add_parser("maintenance", help="clear stale tmp packs and gc.pid, optionally repack "
                                           "(DRY-RUN unless --apply)")
    p.add_argument("--repo", default=None, help="a checkout or git dir (default: current directory)")
    p.add_argument("--apply", action="store_true", help="actually change things (default: print the plan)")
    p.add_argument("--repack", action="store_true",
                   help="with --apply: git repack --geometric=2 -d --write-midx, then verify")
    p.add_argument("--json", action="store_true", dest="as_json")


def _mb(n: Optional[int]) -> str:
    return "?" if n is None else f"{n / 1048576:.1f} MB"


def render_plan(pl: Dict[str, Any], apply_mode: bool) -> List[str]:
    g = pl["garbage"]
    lines = [f"{'APPLY' if apply_mode else 'DRY RUN (nothing will change; pass --apply)'}  {pl['git_dir']}",
             f"packs     {pl['packs']} ({_mb(pl['pack_bytes'])})",
             f"tmp_*     {len(g['files'])} file(s), {_mb(g['bytes'])}; "
             f"{g['qualifying']} would be removed ({_mb(g['qualifying_bytes'])})"]
    for f in g["files"]:
        lines.append(f"  {'REMOVE' if f['qualifies'] else 'keep  '} {f['name']}  {_mb(f['size'])}  "
                     f"age {ci_age(f['age_s'])}  {f['why']}")
    if g.get("note"):
        lines.append(f"  note: {g['note']}")
    gp = pl["gc_pid"]
    lines.append("gc.pid    " + ((("would remove: " if gp.get("stale") else "keep: ") + gp["why"])
                                 if gp.get("present") else "none"))
    for b in pl["bad_caches"]:
        lines.append(f"cache     bad, would be moved aside before a repack: {b}")
    rp = pl["repack"]
    lines.append("repack    " + (("would run: " if not apply_mode else "running: ") + " ".join(rp["argv"])
                                 if rp["requested"] else "not requested (--repack)"))
    if pl["host_lock"]:
        lines.append(f"REFUSE    --apply would be refused: {pl['host_lock']}")
    for w in pl["writers"]:
        lines.append(f"BUSY      {w}")
    for w in pl["warnings"]:
        lines.append(f"WARNING   {w}")
    for u in pl["unjudged"]:
        lines.append(f"UNJUDGED  {u}")
    return lines


def ci_age(s: Any) -> str:
    return ci._fmt_age(s)


def run(args: argparse.Namespace) -> int:
    gd = ci.resolve_git_dir(args.repo)
    if gd is None:
        print(f"awgit maintenance: no git repo at {args.repo or os.getcwd()}", file=sys.stderr)
        return 2
    do_apply = bool(args.apply)
    pl = plan(gd, repack=bool(args.repack))
    if not do_apply:
        print(json.dumps(pl, sort_keys=True) if args.as_json else "\n".join(render_plan(pl, False)))
        return 0
    res = apply(gd, repack=bool(args.repack))
    if args.as_json:
        print(json.dumps({"plan": pl, "result": res}, sort_keys=True))
    else:
        print("\n".join(render_plan(pl, True)))
        if res["refused"]:
            print(f"REFUSED   {res['refused']}", file=sys.stderr)
        else:
            print(f"removed   {len(res['removed'])} file(s), {_mb(res['freed_bytes'])}"
                  f"{'; gc.pid removed' if res['gc_pid_removed'] else ''}")
            rp = res["repack"]
            if rp:
                print("repack    " + ("ok" if rp.get("ok") else f"FAILED: {rp.get('refused') or rp}")
                      + f"  packs {rp.get('packs_before')} -> {rp.get('packs_after')}"
                        f"  verify rc={rp.get('verify_rc')}")
            for e in res["errors"]:
                print(f"ERROR     {e}", file=sys.stderr)
    return 0 if res["ok"] else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="awgit maintenance")
    sub = ap.add_subparsers(dest="cmd")
    add_parser(sub)
    return run(ap.parse_args(["maintenance", *(sys.argv[1:] if argv is None else argv)]))


if __name__ == "__main__":
    raise SystemExit(main())
