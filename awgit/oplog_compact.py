"""``awgit oplog compact``: rewrite ``parent_ops`` to the nearest ancestors only.

Until 2026-11-08 capture recorded EVERY captured first-parent ancestor in each
op's ``parent_ops``, so the log grew quadratically: measured that day, 989 MB
for 12,395 ops, 860 MB of it ``parent_ops``. Capture now records the nearest
captured ancestor (``capture._nearest_captured_ancestors``) and
``OpLog.linearize`` orders by reachability; this tool rewrites the ops already
on disk to the same shape.

Per op it keeps the TRANSITIVE REDUCTION of its parent list: a parent is
dropped only when it is reachable from another kept parent through the
already-reduced graph, so every op's causal closure is unchanged. The rewrite:

1. snapshots the log size under the op-log lock (appends happen under it, so
   the snapshot ends on a line boundary) and streams that prefix into a temp
   file next to the log -- one line in memory at a time, no lock held;
2. verifies the temp file against the original, independently re-reading both:
   the same op ids in the same order, every field but ``parent_ops`` equal,
   each new parent list a subset of the old one, and each old parent list
   inside the new causal closure -- together that is closure equality;
3. ``--apply`` only: takes the op-log lock again (waiting for peers, never
   killing them), reduces and verifies whatever peers appended meanwhile,
   renames the original to ``oplog.jsonl.bak-<UTC>-precompact`` (never deleted
   here), moves the temp file into place and writes a fresh ``oplog.index``.

Lines that are not ops (the header, blanks, a torn line) are copied verbatim,
and an op whose parent list is already minimal keeps its exact bytes.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from awgit.data_root import vcs_data_root
from awgit.oplog import FileLock, _lock_path, index_path_for, write_index

Graph = Dict[str, List[str]]


def reduce_parents(op_id: str, parents: List[str], graph: Graph) -> List[str]:
    """``parents`` minus every entry reachable from another kept entry.

    ``graph`` holds the (already reduced) parent lists of earlier ops. A parent
    the graph does not know (dangling, or written later in the file) is kept:
    without its ancestry nothing can be proven redundant through it.
    """
    kept: List[str] = []
    seen: Set[str] = set()
    covered: Set[str] = set()  # strict ancestors of the kept parents
    for p in parents:
        if p == op_id or p in seen:
            continue
        seen.add(p)
        if p in covered:
            continue
        kept.append(p)
        stack = list(graph.get(p, ()))
        while stack:
            x = stack.pop()
            if x in covered:
                continue
            covered.add(x)
            stack.extend(graph.get(x, ()))
    return [k for k in kept if k not in covered]


def closure(starts: List[str], graph: Graph) -> Set[str]:
    """Every op reachable from ``starts`` (inclusive) through ``graph``."""
    out: Set[str] = set()
    stack = list(starts)
    while stack:
        x = stack.pop()
        if x in out:
            continue
        out.add(x)
        stack.extend(graph.get(x, ()))
    return out


def _eol(raw: bytes) -> bytes:
    return b"\r\n" if raw.endswith(b"\r\n") else b"\n"


def _parse(raw: bytes) -> Optional[Dict[str, Any]]:
    line = raw.strip()
    if not line or line.startswith(b"#") or not raw.endswith(b"\n"):
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict) or "op_id" not in d:
        return None
    return d


class _Rewriter:
    """Streams original lines into the compacted file, tracking index rows."""

    def __init__(self, out_path: Path) -> None:
        self.out_path = out_path
        self.graph: Graph = {}
        self.rows: List[Tuple[str, str, int, int]] = []
        self.ops = 0
        self.lines = 0
        self.parent_ids_before = 0
        self.parent_ids_after = 0
        self.max_parents_before = 0
        self.max_parents_after = 0
        self._fh = open(out_path, "wb")
        self.pos = 0

    def feed(self, src: Path, start: int, end: int) -> None:
        with open(src, "rb") as f:
            f.seek(start)
            pos = start
            for raw in f:
                if pos >= end:
                    break
                pos += len(raw)
                self.lines += 1
                d = _parse(raw)
                if d is None:
                    self._write(raw)
                    continue
                op_id = str(d["op_id"])
                old = [str(p) for p in d.get("parent_ops") or []]
                new = reduce_parents(op_id, old, self.graph)
                self.graph[op_id] = new
                self.ops += 1
                self.parent_ids_before += len(old)
                self.parent_ids_after += len(new)
                self.max_parents_before = max(self.max_parents_before, len(old))
                self.max_parents_after = max(self.max_parents_after, len(new))
                if new == old:
                    out = raw
                else:
                    d["parent_ops"] = new
                    out = json.dumps(d).encode("utf-8") + _eol(raw)
                at = self.pos
                self._write(out)
                self.rows.append((op_id, str(d.get("git_sha") or ""), at, self.pos))
            if pos != end:
                raise RuntimeError(
                    f"{src} changed under the rewrite: read to byte {pos}, expected {end}")

    def _write(self, data: bytes) -> None:
        self._fh.write(data)
        self.pos += len(data)

    def close(self) -> None:
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()


def _iter_lines(path: Path, start: int, end: int) -> Iterator[bytes]:
    with open(path, "rb") as f:
        f.seek(start)
        pos = start
        for raw in f:
            if pos >= end:
                return
            pos += len(raw)
            yield raw


def verify(old_path: Path, old_range: Tuple[int, int],
           new_path: Path, new_range: Tuple[int, int],
           graph: Optional[Graph] = None) -> Dict[str, Any]:
    """Independently re-read both files and prove the rewrite lossless.

    ``graph`` defaults to one built from ``new_path`` itself (a first pass),
    so the check does not trust the rewriter's memory.
    """
    if graph is None:
        graph = {}
        for raw in _iter_lines(new_path, *new_range):
            d = _parse(raw)
            if d is not None:
                graph[str(d["op_id"])] = [str(p) for p in d.get("parent_ops") or []]
    problems: List[str] = []
    ops = lines = 0
    old_iter = _iter_lines(old_path, *old_range)
    new_iter = _iter_lines(new_path, *new_range)
    for old_raw in old_iter:
        lines += 1
        new_raw = next(new_iter, None)
        if new_raw is None:
            problems.append(f"line {lines}: missing from the rewrite")
            break
        a, b = _parse(old_raw), _parse(new_raw)
        if a is None or b is None:
            if (a is None) != (b is None) or old_raw != new_raw:
                problems.append(f"line {lines}: a non-op line changed")
            continue
        ops += 1
        oid = str(a["op_id"])
        old_p = [str(p) for p in a.pop("parent_ops", None) or []]
        new_p = [str(p) for p in b.pop("parent_ops", None) or []]
        if a != b:
            problems.append(f"op {oid}: a field other than parent_ops changed")
        if not set(new_p) <= set(old_p):
            problems.append(f"op {oid}: the rewrite added a parent")
        missing = set(old_p) - closure(new_p, graph) - {oid}
        if missing:
            problems.append(f"op {oid}: {len(missing)} ancestors lost from its closure")
        if len(problems) > 20:
            break
    if next(new_iter, None) is not None:
        problems.append("the rewrite has extra lines")
    return {"ops": ops, "lines": lines, "problems": problems}


@contextlib.contextmanager
def patient_lock(path: Path, timeout: float) -> Iterator[None]:
    """The op-log lock, waiting out peers (Windows raises after ~10 s of retries)."""
    deadline = time.monotonic() + timeout
    delay = 1.0
    while True:
        lock = FileLock(path)
        try:
            lock.__enter__()
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 15.0)
            continue
        break
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def _replace_with_retry(src: Path, dst: Path, attempts: int = 20) -> None:
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            # A virus scanner or indexer holding a handle; peers only open the
            # log under the lock we hold.
            if i == attempts - 1:
                raise
            time.sleep(0.5)


def compact(data_root: Optional[Path] = None, *, apply: bool = False,
            lock_timeout: float = 900.0) -> Dict[str, Any]:
    """Compact ``oplog.jsonl`` in ``data_root``; see the module docstring."""
    data = Path(data_root) if data_root else vcs_data_root()
    log = data / "oplog.jsonl"
    lock = _lock_path(data)
    report: Dict[str, Any] = {"oplog": str(log), "applied": False}
    if not log.exists():
        report["error"] = "no op-log"
        return report
    t0 = time.monotonic()
    with patient_lock(lock, lock_timeout):
        snap = log.stat().st_size
    report["bytes_before"] = snap
    tmp = log.with_name(log.name + (".compact.tmp" if apply else ".compact-dryrun.tmp"))
    rw = _Rewriter(tmp)
    try:
        rw.feed(log, 0, snap)
        rw.close()
        check = verify(log, (0, snap), tmp, (0, rw.pos))
        report.update(verify_ops=check["ops"], verify_lines=check["lines"],
                      problems=list(check["problems"]))
        if check["ops"] != rw.ops or check["lines"] != rw.lines:
            report["problems"].append(
                f"count mismatch: rewrote {rw.ops} ops / {rw.lines} lines, "
                f"verified {check['ops']} / {check['lines']}")
        if apply and not report["problems"]:
            with patient_lock(lock, lock_timeout):
                now = log.stat().st_size
                if now < snap:
                    raise RuntimeError(f"{log} shrank under the rewrite ({snap} -> {now})")
                if now > snap:
                    pre = rw.pos
                    rw._fh = open(tmp, "ab")
                    rw.feed(log, snap, now)
                    rw.close()
                    tail = verify(log, (snap, now), tmp, (pre, rw.pos), graph=rw.graph)
                    report["tail_ops"] = tail["ops"]
                    report["problems"].extend(tail["problems"])
                if not report["problems"]:
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    backup = log.with_name(f"{log.name}.bak-{stamp}-precompact")
                    _replace_with_retry(log, backup)
                    try:
                        _replace_with_retry(tmp, log)
                    except BaseException:
                        _replace_with_retry(backup, log)
                        raise
                    try:
                        write_index(index_path_for(log), rw.rows)
                    except OSError as exc:  # a cache: the next open rebuilds it
                        report["index_error"] = str(exc)
                    report.update(applied=True, backup=str(backup), bytes_before=now)
    finally:
        if not rw._fh.closed:
            rw._fh.close()
        if tmp.exists() and not report["applied"]:
            tmp.unlink()  # our own temp file, never the log or a backup
    report.update(
        ops=rw.ops, lines=rw.lines, bytes_after=rw.pos,
        parent_ids_before=rw.parent_ids_before, parent_ids_after=rw.parent_ids_after,
        max_parents_before=rw.max_parents_before, max_parents_after=rw.max_parents_after,
        seconds=round(time.monotonic() - t0, 1),
    )
    return report
