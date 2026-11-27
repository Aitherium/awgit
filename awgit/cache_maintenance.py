"""``awgit maintenance``: clear a repo's leftovers, consolidate packs, refresh the commit-graph.

DEFAULT IS A DRY RUN. It prints the plan -- pack count and bytes, tmp garbage with
sizes and ages, gc.pid state, shallow state, what a repack / graph write would do --
and changes nothing.

``--apply`` takes ``<git-dir>/awgit-cache.lock`` (the same lock ``awgit caches
--fix`` and the guard use, so there is one writer of the derived caches at a time),
then removes only what ``cache_integrity`` says qualifies:

* ``tmp_pack_*`` / ``tmp_idx_*`` / ``tmp_rev_*`` older than 6 h and not held open
  by any process (24 h when open handles cannot be judged);
* a ``gc.pid`` older than 12 h, or whose pid is dead on this host.

``--apply --repack`` additionally consolidates packs, chosen by the repo's kind:

* ordinary repo: ``git repack --geometric=2 -d --write-midx`` (niced);
* PARTIAL / PROMISOR clone: that command dies with ``cannot use internal rev list
  with --stdin-packs``, so it is never tried. Instead the supported
  ``multi-pack-index repack --batch-size=<N>`` then ``multi-pack-index expire``, at
  most three rounds (1 GiB, 512 MiB, 256 MiB), ``multi-pack-index verify`` and a
  HEAD-resolves check after the repack and again after the expire, stopping when
  the pack count stops dropping. ``--batch-size=0`` would rewrite EVERY pack
  (including a multi-GiB one) and is refused in code, not just avoided.

``--apply --commit-graph`` runs ``git commit-graph write --split --reachable
--changed-paths`` (an incremental layer, seconds); ``--full`` makes it
``--split=replace`` (rewrites the chain; weekly is plenty). It is SKIPPED, loudly,
in a shallow repo: git silently ignores the graph there and writes nothing while
exiting 0. Afterwards ``git commit-graph verify`` and a check that every layer
still carries the changed-path filters.

Every writer step refuses while another git gc / repack / multi-pack-index /
commit-graph writer is running. Everything refuses while the HOST's
``wsl-maintenance.lock`` is fresh. That lock belongs to the host (a disk move or
VM restart in progress), so like ``fleet_verbs`` it is NOT relocated by
``AITHER_HOME``; ``AITHER_MAINT_LOCK`` overrides it for tests.

Nothing here schedules itself.
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
#: rounds of ``multi-pack-index repack``: batch sizes in bytes. Never 0 (see module doc).
MIDX_BATCHES = (1 << 30, 512 << 20, 256 << 20)
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


def _nice() -> List[str]:
    pre: List[str] = []
    if shutil.which("nice"):
        pre += ["nice", "-n", "19"]
    if shutil.which("ionice"):
        pre += ["ionice", "-c3"]
    return pre


def _git(git_dir: Path, *args: str) -> List[str]:
    return ["git", "--git-dir", str(git_dir), *args]


def repack_argv(git_dir: Path) -> List[str]:
    """The geometric repack (ordinary repos only)."""
    return _nice() + _git(git_dir, "-c", "pack.threads=2", "repack", "--geometric=2", "-d", "--write-midx")


def midx_repack_argv(git_dir: Path, batch: int) -> List[str]:
    if batch <= 0:
        # --batch-size=0 means "every pack": it would rewrite the biggest pack too
        raise ValueError(f"refusing multi-pack-index repack --batch-size={batch}")
    return _nice() + _git(git_dir, "multi-pack-index", "repack", f"--batch-size={batch}", "--no-progress")


def midx_expire_argv(git_dir: Path) -> List[str]:
    return _nice() + _git(git_dir, "multi-pack-index", "expire", "--no-progress")


def commit_graph_argv(git_dir: Path, full: bool = False) -> List[str]:
    return _nice() + _git(git_dir, "commit-graph", "write", "--split=replace" if full else "--split",
                          "--reachable", "--changed-paths", "--no-progress")


def plan(git_dir: Path, *, repack: bool = False, commit_graph: bool = False, full: bool = False,
         now: Optional[float] = None) -> Dict[str, Any]:
    """Everything maintenance would do, as data. Read-only."""
    rep = ci.scan(git_dir, now=now)
    g = rep["garbage"]
    partial = rep["partial_clone"]
    return {"git_dir": str(git_dir), "packs": rep["packs"]["count"], "pack_bytes": _pack_bytes(git_dir),
            "garbage": g, "gc_pid": rep["gc_pid"], "caches": rep["caches"],
            "bad_caches": [v["path"] for v in rep["caches"] if v["state"] == ci.BAD],
            "partial_clone": partial, "shallow": rep["shallow"], "midx": rep["midx"],
            "repack": {"requested": repack,
                       "mode": "midx-batch" if partial["partial"] else "geometric",
                       "argv": (midx_repack_argv(git_dir, MIDX_BATCHES[0]) if partial["partial"]
                                else repack_argv(git_dir)),
                       "rounds": [b for b in MIDX_BATCHES] if partial["partial"] else None},
            "commit_graph": {"requested": commit_graph or full, "mode": "full" if full else "incremental",
                             "argv": commit_graph_argv(git_dir, full), "current": rep["commit_graph"],
                             "skip": ci.shallow_message(rep["shallow"]) if rep["shallow"].get("shallow") else ""},
            "host_lock": host_maintenance_fresh(now), "writers": writers_running(),
            "warnings": rep["warnings"], "unjudged": rep["unjudged"]}


def _runner(run_cmd: Any) -> Any:
    return run_cmd or (lambda argv: subprocess.run(argv).returncode)


def apply(git_dir: Path, *, repack: bool = False, commit_graph: bool = False, full: bool = False,
          now: Optional[float] = None, run_cmd: Any = None,
          batches: Optional[Sequence[int]] = None) -> Dict[str, Any]:
    """Execute the plan under the cache lock. ``run_cmd(argv) -> returncode`` is a test seam."""
    res: Dict[str, Any] = {"ok": False, "refused": "", "removed": [], "freed_bytes": 0,
                           "gc_pid_removed": False, "healed": [], "repack": None,
                           "commit_graph": None, "errors": []}
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
        steps_ok = True
        if repack or commit_graph or full:
            # BEFORE touching anything: with another writer running, even moving a
            # bad cache aside is not ours to do
            busy = _busy_or_live_gc(git_dir)
            if busy:
                if repack:
                    res["repack"] = {"ok": False, "refused": busy}
                if commit_graph or full:
                    res["commit_graph"] = {"ok": False, "refused": busy}
                steps_ok = False
        if (repack or commit_graph or full) and steps_ok:
            # a bad MIDX / graph would be read straight through by the writers below
            healed = ci.heal(git_dir, lock=lk, trigger="awgit maintenance")
            res["healed"] = healed["moved"]
            if not healed["ok"]:
                res["errors"].append("could not move a bad cache aside")
                steps_ok = False
        if repack and steps_ok:
            res["repack"] = _repack(git_dir, run_cmd, batches)
            steps_ok = bool(res["repack"].get("ok"))
        elif repack and res["repack"] is None:
            res["repack"] = {"ok": False, "refused": "skipped: a bad cache could not be moved aside"}
        if (commit_graph or full) and steps_ok:
            res["commit_graph"] = _commit_graph(git_dir, run_cmd, full)
        elif (commit_graph or full) and res["commit_graph"] is None:
            res["commit_graph"] = {"ok": False, "skipped": "an earlier step failed"}
        if (res["removed"] or res["gc_pid_removed"] or res["repack"] or res["commit_graph"]
                or res["healed"]):
            ci.record({"action": "maintenance", "git_dir": str(git_dir), "removed": res["removed"],
                       "freed_bytes": res["freed_bytes"], "gc_pid_removed": res["gc_pid_removed"],
                       "repack": res["repack"], "commit_graph": res["commit_graph"]})
        res["ok"] = (not res["errors"]
                     and (res["repack"] is None or bool(res["repack"].get("ok")))
                     and (res["commit_graph"] is None or bool(res["commit_graph"].get("ok"))))
        return res
    finally:
        lk.release()


def _busy_or_live_gc(git_dir: Path) -> str:
    busy = writers_running()
    if busy:
        return "another git writer is running: " + "; ".join(busy[:3])
    gp = ci.gc_pid_state(git_dir)
    if gp.get("present") and not gp.get("stale"):
        return f"gc.pid is live or recent ({gp['why']}): a gc may be running"
    return ""


def _head_resolves(git_dir: Path, runner: Any) -> bool:
    return runner(_git(git_dir, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")) == 0


def _repack(git_dir: Path, run_cmd: Any, batches: Optional[Sequence[int]]) -> Dict[str, Any]:
    partial, why = ci.is_partial_clone(git_dir)
    if partial:
        return _repack_midx(git_dir, run_cmd, batches or MIDX_BATCHES, why)
    out: Dict[str, Any] = {"ok": False, "mode": "geometric", "argv": repack_argv(git_dir)}
    refuse = _busy_or_live_gc(git_dir)
    if refuse:
        out["refused"] = refuse
        return out
    runner = _runner(run_cmd)
    out["packs_before"] = ci.pack_count(git_dir)
    t0 = time.time()
    out["repack_rc"] = runner(out["argv"])
    out["seconds"] = round(time.time() - t0)
    out["packs_after"] = ci.pack_count(git_dir)
    if out["repack_rc"] != 0:
        out["refused"] = f"repack exited {out['repack_rc']}; old packs are untouched until the new ones are written"
        return out
    out["verify_rc"] = runner(_git(git_dir, "multi-pack-index", "verify"))
    out["quick"] = [v for v in ci.check_caches(git_dir) if v["state"] == ci.BAD]
    out["ok"] = out["verify_rc"] == 0 and not out["quick"]
    return out


def _repack_midx(git_dir: Path, run_cmd: Any, batches: Sequence[int], why: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "mode": "midx-batch", "partial_clone": why, "rounds": [],
                           "packs_before": ci.pack_count(git_dir)}
    for b in batches:
        midx_repack_argv(git_dir, b)          # raises on a batch <= 0, before anything runs
    runner = _runner(run_cmd)
    refuse = _busy_or_live_gc(git_dir)
    if refuse:
        out["refused"] = refuse
        return out
    head_ok = _head_resolves(git_dir, runner)  # skip the resolve check on an unborn HEAD
    t_all = time.time()

    def verified(row: Dict[str, Any], key: str) -> bool:
        row[key] = runner(_git(git_dir, "multi-pack-index", "verify", "--no-progress"))
        bad = [v for v in ci.check_caches(git_dir) if v["state"] == ci.BAD]
        resolves = (not head_ok) or _head_resolves(git_dir, runner)
        if row[key] != 0 or bad or not resolves:
            row["failed"] = (f"multi-pack-index verify rc={row[key]}" if row[key] != 0 else
                             "a cache file is bad" if bad else "HEAD no longer resolves")
            return False
        return True

    for batch in batches[:3]:
        refuse = _busy_or_live_gc(git_dir)
        if refuse:
            out["refused"] = refuse
            return out
        row: Dict[str, Any] = {"batch": batch, "packs_before": ci.pack_count(git_dir)}
        out["rounds"].append(row)
        t0 = time.time()
        # `multi-pack-index repack` only selects packs the MIDX LISTS. Lazy promisor
        # fetches keep adding packs the MIDX has never heard of, so without a refresh
        # the repack consolidates nothing (a live repo sat at 28 -> 28). Compare the
        # .idx names on disk with the MIDX's PNAM chunk (file reads only) and rewrite
        # it first when any is missing, or when there is no MIDX at all. Checked in
        # every round: a fetch can land a pack between two of them.
        mx = ci.midx_state(git_dir)
        row["midx_missing"] = mx["missing_count"] if mx["present"] else None
        if not mx["present"] or mx["missing_count"]:
            row["midx_write_rc"] = runner(_git(git_dir, "multi-pack-index", "write", "--no-progress"))
            if row["midx_write_rc"] != 0:
                out["refused"] = f"multi-pack-index write exited {row['midx_write_rc']}"
                return out
            if not verified(row, "verify_after_write_rc"):
                out["refused"] = f"after multi-pack-index write: {row['failed']}; repack NOT run"
                return out
        row["repack_rc"] = runner(midx_repack_argv(git_dir, batch))
        if row["repack_rc"] != 0:
            out["refused"] = f"multi-pack-index repack exited {row['repack_rc']}"
            return out
        if not verified(row, "verify_rc"):
            out["refused"] = f"after repack: {row['failed']}; expire NOT run, old packs kept"
            return out
        row["expire_rc"] = runner(midx_expire_argv(git_dir))
        if row["expire_rc"] != 0:
            out["refused"] = f"multi-pack-index expire exited {row['expire_rc']}"
            return out
        if not verified(row, "verify_after_expire_rc"):
            out["refused"] = f"after expire: {row['failed']}"
            return out
        row["packs_after"] = ci.pack_count(git_dir)
        row["seconds"] = round(time.time() - t0)
        before, after = row["packs_before"], row["packs_after"]
        if after is None or before is None or after >= before or after <= ci.pack_warn_threshold():
            break   # stopped dropping, or already under the threshold
    out["packs_after"] = ci.pack_count(git_dir)
    out["seconds"] = round(time.time() - t_all)
    out["ok"] = True
    return out


def _commit_graph(git_dir: Path, run_cmd: Any, full: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "mode": "full" if full else "incremental",
                           "argv": commit_graph_argv(git_dir, full)}
    sh = ci.shallow_state(git_dir)
    if sh.get("shallow"):
        # git exits 0 and writes nothing here; reporting "ok" would be the lie
        out["skipped"] = ci.shallow_message(sh)
        return out
    refuse = _busy_or_live_gc(git_dir)
    if refuse:
        out["refused"] = refuse
        return out
    runner = _runner(run_cmd)
    before = ci.commit_graph_layers(git_dir)
    t0 = time.time()
    out["write_rc"] = runner(out["argv"])
    out["seconds"] = round(time.time() - t0)
    if out["write_rc"] != 0:
        out["refused"] = f"commit-graph write exited {out['write_rc']}"
        return out
    out["verify_rc"] = runner(_git(git_dir, "commit-graph", "verify", "--no-progress"))
    layers = ci.commit_graph_layers(git_dir)
    out.update({"layers_before": len(before), "layers": len(layers),
                "bytes": sum(x["size"] for x in layers),
                "filters": bool(layers) and all(x["filters"] for x in layers),
                "quick": [v for v in ci.check_caches(git_dir) if v["state"] == ci.BAD]})
    if not layers:
        out["refused"] = "git wrote no commit-graph (rc 0, nothing on disk)"
    elif out["verify_rc"] != 0 or out["quick"]:
        out["refused"] = f"commit-graph verify rc={out['verify_rc']}" if out["verify_rc"] else "a cache file is bad"
    elif not out["filters"]:
        out["refused"] = "a layer lacks the changed-path filters (check_commit_graph_fresh would call it STALE)"
    else:
        out["ok"] = True
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────

def add_parser(sub: Any) -> None:
    p = sub.add_parser("maintenance", help="clear stale tmp packs and gc.pid, optionally repack and "
                                           "refresh the commit-graph (DRY-RUN unless --apply)")
    p.add_argument("--repo", default=None, help="a checkout or git dir (default: current directory)")
    p.add_argument("--apply", action="store_true", help="actually change things (default: print the plan)")
    p.add_argument("--repack", action="store_true",
                   help="with --apply: consolidate packs (geometric repack; multi-pack-index "
                        "repack + expire on a partial/promisor clone), then verify")
    p.add_argument("--commit-graph", action="store_true", dest="commit_graph",
                   help="with --apply: write an incremental commit-graph layer with changed-path "
                        "filters, then verify (skipped in a shallow repo)")
    p.add_argument("--full", action="store_true",
                   help="with --apply: rewrite the whole commit-graph chain (--split=replace); "
                        "implies --commit-graph")
    p.add_argument("--json", action="store_true", dest="as_json")


def _mb(n: Optional[int]) -> str:
    return "?" if n is None else f"{n / 1048576:.1f} MB"


def render_plan(pl: Dict[str, Any], apply_mode: bool) -> List[str]:
    g = pl["garbage"]
    lines = [f"{'APPLY' if apply_mode else 'DRY RUN (nothing will change; pass --apply)'}  {pl['git_dir']}",
             f"packs     {pl['packs']} ({_mb(pl['pack_bytes'])})"
             + (f"  [partial clone: {', '.join(pl['partial_clone']['why'])}]"
                if pl["partial_clone"]["partial"] else ""),
             f"tmp_*     {len(g['files'])} file(s), {_mb(g['bytes'])}; "
             f"{g['qualifying']} would be removed ({_mb(g['qualifying_bytes'])})"]
    for f in g["files"]:
        lines.append(f"  {'REMOVE' if f['qualifies'] else 'keep  '} {f['name']}  {_mb(f['size'])}  "
                     f"age {ci._fmt_age(f['age_s'])}  {f['why']}")
    if g.get("note"):
        lines.append(f"  note: {g['note']}")
    gp = pl["gc_pid"]
    lines.append("gc.pid    " + ((("would remove: " if gp.get("stale") else "keep: ") + gp["why"])
                                 if gp.get("present") else "none"))
    mx = pl.get("midx") or {}
    if mx.get("present") and mx.get("judged"):
        lines.append(f"midx      lists {mx['listed']} of {mx['on_disk']} pack(s)"
                     + (f"; {mx['missing_count']} unlisted: a partial-clone --repack rewrites it first "
                        f"(`multi-pack-index write`), else the repack has nothing to select"
                        if mx["missing_count"] else ""))
    for b in pl["bad_caches"]:
        lines.append(f"cache     bad, would be moved aside before a repack/graph write: {b}")
    rp = pl["repack"]
    verb = "running" if apply_mode else "would run"
    if not rp["requested"]:
        lines.append("repack    not requested (--repack)")
    elif rp["mode"] == "midx-batch":
        sizes = ", ".join(_mb(b) for b in rp["rounds"])
        lines.append(f"repack    partial clone: geometric repack fails here; {verb} multi-pack-index repack "
                     f"--batch-size in rounds ({sizes}), each verified, then expire:")
        lines.append("          " + " ".join(rp["argv"]))
    else:
        lines.append(f"repack    {verb}: " + " ".join(rp["argv"]))
    cg = pl["commit_graph"]
    cur = cg["current"]
    if not cg["requested"]:
        lines.append(f"graph     {cur['layers']} layer(s), filters {'yes' if cur['filters'] else 'NO'}; "
                     f"not requested (--commit-graph)")
    elif cg["skip"]:
        lines.append(f"graph     SKIP: {cg['skip']}")
    else:
        lines.append(f"graph     {cg['mode']}; {verb}: " + " ".join(cg["argv"]))
    if pl["shallow"].get("shallow"):
        lines.append(f"SHALLOW   {ci.shallow_message(pl['shallow'])}")
    if pl["host_lock"]:
        lines.append(f"REFUSE    --apply would be refused: {pl['host_lock']}")
    for w in pl["writers"]:
        lines.append(f"BUSY      {w}")
    for w in pl["warnings"]:
        lines.append(f"WARNING   {w}")
    for u in pl["unjudged"]:
        lines.append(f"UNJUDGED  {u}")
    return lines


def _render_result(res: Dict[str, Any]) -> List[str]:
    lines = [f"removed   {len(res['removed'])} file(s), {_mb(res['freed_bytes'])}"
             f"{'; gc.pid removed' if res['gc_pid_removed'] else ''}"]
    rp = res["repack"]
    if rp:
        if rp.get("mode") == "midx-batch":
            lines.append("repack    " + ("ok" if rp.get("ok") else f"FAILED: {rp.get('refused')}")
                         + f"  packs {rp.get('packs_before')} -> {rp.get('packs_after')} "
                           f"in {len(rp['rounds'])} round(s)")
            for r in rp["rounds"]:
                mxn = ("; midx rewritten first (" + ("none" if r.get("midx_missing") is None
                                                       else f"{r['midx_missing']} pack(s) unlisted") + ")"
                       if "midx_write_rc" in r else "")
                lines.append(f"  batch {_mb(r['batch'])}: packs {r.get('packs_before')} -> {r.get('packs_after')}  "
                             f"verify rc={r.get('verify_rc')}/{r.get('verify_after_expire_rc')}{mxn}")
        else:
            lines.append("repack    " + ("ok" if rp.get("ok") else f"FAILED: {rp.get('refused') or rp}")
                         + f"  packs {rp.get('packs_before')} -> {rp.get('packs_after')}"
                           f"  verify rc={rp.get('verify_rc')}")
    cg = res["commit_graph"]
    if cg:
        if cg.get("ok"):
            lines.append(f"graph     ok ({cg['mode']}): {cg['layers_before']} -> {cg['layers']} layer(s), "
                         f"{_mb(cg['bytes'])}, filters yes, {cg['seconds']}s")
        else:
            lines.append(f"graph     NOT DONE: {cg.get('skipped') or cg.get('refused') or cg}")
    return lines


def run(args: argparse.Namespace) -> int:
    gd = ci.resolve_git_dir(args.repo)
    if gd is None:
        print(f"awgit maintenance: no git repo at {args.repo or os.getcwd()}", file=sys.stderr)
        return 2
    do_apply = bool(args.apply)
    full = bool(getattr(args, "full", False))
    graph = bool(getattr(args, "commit_graph", False)) or full
    pl = plan(gd, repack=bool(args.repack), commit_graph=graph, full=full)
    if not do_apply:
        print(json.dumps(pl, sort_keys=True) if args.as_json else "\n".join(render_plan(pl, False)))
        return 0
    res = apply(gd, repack=bool(args.repack), commit_graph=graph, full=full)
    if args.as_json:
        print(json.dumps({"plan": pl, "result": res}, sort_keys=True))
    else:
        print("\n".join(render_plan(pl, True)))
        if res["refused"]:
            print(f"REFUSED   {res['refused']}", file=sys.stderr)
        else:
            print("\n".join(_render_result(res)))
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
