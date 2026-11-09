"""Durable append-only op-log for the semantic-VCS layer.

An ``EditOp`` is one commit's semantic annotation. The log is append-only
JSONL — each line one ``EditOp.to_dict()`` — under an OS-level exclusive lock
with an fsync on every append. It is NOT recomputable (ops carry summaries), so
``export`` is the durability story; a fresh machine clones + reindexes and
optionally imports a shipped export.

The post-commit hook runs as its own sync process, so ``FileLock`` below is a
plain blocking call reached only from sync code (see the comment there).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from threading import RLock
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from awgit.data_root import vcs_data_root
from awgit.schema import EditOp

_HEADER = "# vcs-oplog v1"


def _lock_path(data_root: Path) -> Path:
    return data_root / "oplog.lock"


# Per-path process-local mutexes guarding the cross-process file locks.
# Windows msvcrt ``LK_LOCK`` retries ~10x then raises EDEADLK under dense
# INTRA-process thread contention (20 threads, one process, one lock byte) —
# cross-process locking works (separate processes block), but threads in ONE
# process trip the retry limit. The mutex serializes threads in-process; the
# file lock still serializes across processes (the real post-commit-hook case:
# concurrent captures from separate hook processes). Caught by the M6
# concurrent-put test; M1's threaded append test passed only by looser
# contention.
_PROCESS_LOCKS: Dict[str, "threading.Lock"] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def _process_lock(path: Path) -> "threading.Lock":
    key = str(path.resolve())
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


class FileLock:
    """Cross-process exclusive lock (msvcrt on Windows, fcntl on POSIX),
    serialized within the process by a per-path mutex.

    blocking-ok: OS-level advisory lock; reached only from the sync CLI/hook
    path, never from an event loop.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh = None
        self._plock = _process_lock(path)

    def __enter__(self) -> "FileLock":
        self._plock.acquire()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self._path, "a+b")
            if self._fh.seek(0, os.SEEK_END) == 0:
                self._fh.write(b"\0")
                self._fh.flush()
            self._fh.seek(0)
            try:
                import msvcrt

                msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
            except ImportError:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except BaseException:
            self._plock.release()
            raise
        return self

    def __exit__(self, *exc) -> bool:
        try:
            if self._fh is not None:
                try:
                    import msvcrt

                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except ImportError:
                    import fcntl

                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
                self._fh = None
        finally:
            self._plock.release()
        return False


_INDEX_NAME = "oplog.index"
_INDEX_MAGIC = b"# awgit-oplog-index v1"
#: How far into a line its ``op_id`` must appear for an index row to verify.
#: ``EditOp.to_dict`` writes it first, so it sits at byte 12.
_OP_ID_PROBE_BYTES = 256

#: One index row: (start byte, end byte, git_sha) of an op's line.
_Row = Tuple[int, int, str]


def _op_id_needle(op_id: str) -> bytes:
    return b'"op_id": ' + json.dumps(op_id).encode("utf-8")


def index_path_for(log_path: Path) -> Path:
    """The sidecar index next to an op-log file."""
    return Path(log_path).with_name(_INDEX_NAME)


def write_index(index_path: Path, rows: Iterable[Tuple[str, str, int, int]]) -> str:
    """Write a complete fresh index (new generation) atomically; returns the generation.

    ``rows`` are ``(op_id, git_sha, start, end)`` in ascending ``start`` order.
    """
    gen = os.urandom(8).hex()
    tmp = index_path.with_name(index_path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(_INDEX_MAGIC + b" " + gen.encode() + b"\n")
        for op_id, sha, start, end in rows:
            f.write(f"{op_id} {sha or '-'} {start} {end}\n".encode("utf-8"))
    os.replace(tmp, index_path)
    return gen


class OpLog:
    """Append-only semantic op log backed by one JSONL file.

    Opening a log reads only a small sidecar index (``oplog.index``, one
    ``op_id git_sha start end`` row per op line of ``oplog.jsonl``), so the hot
    callers -- the post-commit capture, ``has_commit``, ``sha_index``,
    ``append`` -- never parse an op. ``get``/``ops_for_commit`` seek to one line.
    Only whole-log queries (``all_ops``, ``linearize``, ``export``, ...) parse
    every op, once, on first use. Measured 2026-11-08: parsing every op at
    construction held 2.3-3.3 GB in the hourly ``capture --range`` against a
    989 MB log, and ``has_commit`` scanned every op per call.

    The index is a cache, never the truth. It is extended under the op-log lock
    from wherever it stops (an older awgit appends without updating it), and
    rebuilt from the log when its last row no longer names the line at its
    offset (the log was rewritten, e.g. by ``awgit oplog compact``). The format
    of ``oplog.jsonl`` is unchanged, so every older reader keeps working.
    """

    def __init__(self, data_root: Optional[Path] = None) -> None:
        self._data_root = data_root or vcs_data_root()
        self.path = self._data_root / "oplog.jsonl"
        self.index_path = index_path_for(self.path)
        self._lock = RLock()
        self._ops: Dict[str, EditOp] = {}  # parsed ops (all of them once _loaded)
        self._loaded = False
        self._reset_index()
        if self.path.exists():
            with FileLock(_lock_path(self._data_root)):
                self._refresh_locked()

    # ── index ──────────────────────────────────────────────────────────

    def _reset_index(self) -> None:
        self._rows: Dict[str, _Row] = {}  # op_id -> its (last) line
        self._by_sha: Dict[str, List[str]] = {}  # git_sha -> op ids, file order
        self._covered = 0  # bytes of oplog.jsonl accounted for
        self._last: Optional[Tuple[str, int, int]] = None  # last row (id, start, end)
        self._idx_gen = ""  # generation of the index file consumed ("" = none)
        self._idx_pos = 0  # bytes of the index file consumed

    def _note(self, op_id: str, git_sha: str, start: int, end: int) -> None:
        if op_id not in self._rows:
            self._by_sha.setdefault(git_sha, []).append(op_id)
        self._rows[op_id] = (start, end, git_sha)
        self._last = (op_id, start, end)

    def _read_index_file(self) -> bool:
        """Consume the sidecar index; False when there is none usable."""
        try:
            fh = open(self.index_path, "rb")
        except OSError:
            return False
        with fh:
            first = fh.readline()
            if not first.startswith(_INDEX_MAGIC + b" ") or not first.endswith(b"\n"):
                return False
            gen = first[len(_INDEX_MAGIC) + 1:].strip().decode("ascii", "replace")
            if not gen:
                return False
            try:
                size = os.fstat(fh.fileno()).st_size
            except OSError:
                return False
            if gen != self._idx_gen or size < self._idx_pos:
                self._reset_index()  # another generation: start over
                self._idx_gen = gen
                self._idx_pos = len(first)
            fh.seek(self._idx_pos)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break  # torn row: the log tail-scan covers it
                parts = raw.split()
                if len(parts) != 4:
                    break
                try:
                    start, end = int(parts[2]), int(parts[3])
                except ValueError:
                    break
                if start < self._covered or end <= start:
                    break  # not monotonic: trust nothing after it
                self._idx_pos += len(raw)
                sha = parts[1].decode("ascii", "replace")
                self._note(parts[0].decode("ascii", "replace"),
                           "" if sha == "-" else sha, start, end)
                self._covered = end
        return True

    def _last_row_valid(self, size: int) -> bool:
        """Does the index's last row still name the line at its offset?"""
        if self._covered > size:
            return False
        if self._last is None:
            return True
        op_id, start, end = self._last
        try:
            with open(self.path, "rb") as f:
                f.seek(start)
                head = f.read(_OP_ID_PROBE_BYTES)
                f.seek(end - 1)
                tail = f.read(1)
        except OSError:
            return False
        return _op_id_needle(op_id) in head and tail == b"\n"

    def _scan_tail(self, size: int) -> List[Tuple[str, str, int, int]]:
        """Index the log lines between ``_covered`` and ``size``, streaming.

        Per-line memory only. Stops at a final line with no newline (a torn
        write); an unparseable complete line is skipped, as no op.
        """
        rows: List[Tuple[str, str, int, int]] = []
        if self._covered >= size:
            return rows
        with open(self.path, "rb") as f:
            f.seek(self._covered)
            pos = self._covered
            for raw in f:
                if not raw.endswith(b"\n"):
                    break
                start, pos = pos, pos + len(raw)
                line = raw.strip()
                if line and not line.startswith(b"#"):
                    try:
                        d = json.loads(line)
                        op_id = str(d["op_id"])
                        sha = str(d.get("git_sha") or "")
                    except (ValueError, KeyError, TypeError):
                        d = None
                    if d is not None:
                        self._note(op_id, sha, start, pos)
                        rows.append((op_id, sha, start, pos))
                self._covered = pos
        return rows

    def _all_rows(self) -> List[Tuple[str, str, int, int]]:
        rows = [(oid, sha, s, e) for oid, (s, e, sha) in self._rows.items()]
        rows.sort(key=lambda r: r[2])
        return rows

    def _refresh_locked(self) -> None:
        """Bring the in-memory index up to the log's current end (lock held)."""
        try:
            size = self.path.stat().st_size
        except OSError:
            self._reset_index()
            return
        usable = self._read_index_file()
        if usable and not self._last_row_valid(size):
            usable = False  # the log was rewritten under this index
        if not usable:
            self._reset_index()
        new_rows = self._scan_tail(size)
        try:
            if not usable:
                self._idx_gen = write_index(self.index_path, self._all_rows())
                self._idx_pos = self.index_path.stat().st_size
            elif new_rows:
                body = b"".join(f"{a} {b or '-'} {c} {d}\n".encode("utf-8")
                                for a, b, c, d in new_rows)
                with open(self.index_path, "ab") as f:
                    f.write(body)
                self._idx_pos += len(body)
        except OSError:
            # A cache: a read-only store just rescans next time.
            self._idx_gen = ""

    def _reindex(self) -> None:
        with FileLock(_lock_path(self._data_root)):
            self._reset_index()
            self._refresh_locked()

    def _read_at(self, op_id: str) -> Optional[EditOp]:
        row = self._rows.get(op_id)
        if row is None:
            return None
        start, end, _sha = row
        try:
            with open(self.path, "rb") as f:
                f.seek(start)
                raw = f.read(end - start)
            d = json.loads(raw)
        except (OSError, ValueError):
            return None
        if not isinstance(d, dict) or d.get("op_id") != op_id:
            return None
        return EditOp.from_dict(d)

    def _ensure_loaded(self) -> None:
        """Parse every op (whole-log queries only), streaming, under the lock."""
        with self._lock:
            if self._loaded:
                return
            ops: Dict[str, EditOp] = {}
            if self.path.exists():
                with FileLock(_lock_path(self._data_root)):
                    self._refresh_locked()
                    with open(self.path, "rb") as f:
                        pos = 0
                        for raw in f:
                            if pos >= self._covered:
                                break
                            pos += len(raw)
                            line = raw.strip()
                            if not line or line.startswith(b"#"):
                                continue
                            try:
                                d = json.loads(line)
                            except ValueError:
                                continue  # a torn line: the index skipped it too
                            op = EditOp.from_dict(d)
                            ops[op.op_id] = op
            self._ops = ops
            self._loaded = True

    # ── persistence ────────────────────────────────────────────────────

    def append(self, op: EditOp) -> None:
        """Append one op durably. Idempotent by op_id (callers may retry)."""
        with self._lock:
            with FileLock(_lock_path(self._data_root)):
                self._data_root.mkdir(parents=True, exist_ok=True)
                fresh = not self.path.exists()
                if fresh:
                    self._reset_index()
                else:
                    self._refresh_locked()  # see what peers appended since
                if op.op_id in self._rows:
                    return
                data = (json.dumps(op.to_dict()) + "\n").encode("utf-8")
                with open(self.path, "ab") as f:
                    f.seek(0, os.SEEK_END)
                    if fresh:
                        f.write((_HEADER + "\n").encode("utf-8"))
                    start = f.tell()
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                end = start + len(data)
                self._note(op.op_id, op.git_sha, start, end)
                self._covered = end
                try:
                    if fresh or not self._idx_gen:
                        self._idx_gen = write_index(self.index_path, self._all_rows())
                        self._idx_pos = self.index_path.stat().st_size
                    else:
                        row = f"{op.op_id} {op.git_sha or '-'} {start} {end}\n"
                        with open(self.index_path, "ab") as f:
                            f.write(row.encode("utf-8"))
                        self._idx_pos += len(row)
                except OSError:
                    self._idx_gen = ""
            self._ops[op.op_id] = op

    # ── queries ────────────────────────────────────────────────────────

    def count(self) -> int:
        """Ops in the log, from the index (no parse)."""
        return len(self._rows)

    def all_ops(self) -> List[EditOp]:
        self._ensure_loaded()
        return list(self._ops.values())

    def ops_for_commit(self, git_sha: str) -> List[EditOp]:
        out = []
        for oid in self._by_sha.get(git_sha, []):
            op = self.get(oid)
            if op is not None:
                out.append(op)
        return out

    def ops_for_node(self, node_id: str) -> List[EditOp]:
        return [
            op for op in self.all_ops()
            if any(nc.node_id == node_id for nc in op.node_changes)
        ]

    def ops_since(self, ts: str) -> List[EditOp]:
        return [op for op in self.all_ops() if op.ts >= ts]

    def ops_by(self, actor: str) -> List[EditOp]:
        return [op for op in self.all_ops() if op.actor == actor]

    def sha_index(self) -> Dict[str, str]:
        """git_sha -> op_id (the latest op for that commit). O(index), no parse."""
        return {sha: ids[-1] for sha, ids in self._by_sha.items() if ids}

    def has_commit(self, git_sha: str) -> bool:
        """O(1): is there an op for this commit?"""
        return git_sha in self._by_sha

    def get(self, op_id: str) -> Optional[EditOp]:
        with self._lock:
            op = self._ops.get(op_id)
            if op is not None or self._loaded:
                return op
            if op_id not in self._rows:
                return None
            op = self._read_at(op_id)
            if op is None:
                # The line at the recorded offset is not this op any more: the
                # log was rewritten under us. Re-index and look once more.
                self._reindex()
                op = self._read_at(op_id)
            if op is not None:
                self._ops[op_id] = op
            return op

    def linearize(self, op_ids: Iterable[str]) -> List[EditOp]:
        """Order ops so causal ancestors precede descendants (stable).

        Ordering uses REACHABILITY through the whole op graph, not only the
        edges between wanted ops: ``parent_ops`` holds the nearest captured
        ancestors (a transitive reduction), so wanted A and C are ordered even
        when the only path between them runs through an unwanted B. Ops written
        before the reduction carry every ancestor; that is the same order.
        """
        self._ensure_loaded()
        want = {oid for oid in op_ids if oid in self._ops}
        if not want:
            return []
        # nearest[x] = the wanted ops reachable from x's parents without passing
        # through another wanted op. Iterative post-order DFS, memoised.
        nearest: Dict[str, FrozenSet[str]] = {}
        on_path: Set[str] = set()

        def parents_of(oid: str) -> List[str]:
            op = self._ops.get(oid)
            return [p for p in op.parent_ops if p in self._ops] if op else []

        for root in sorted(want):
            if root in nearest:
                continue
            stack: List[Tuple[str, int]] = [(root, 0)]
            on_path.add(root)
            while stack:
                oid, i = stack[-1]
                ps = parents_of(oid)
                if i < len(ps):
                    stack[-1] = (oid, i + 1)
                    p = ps[i]
                    if p in want or p in nearest or p in on_path:
                        continue  # wanted: a boundary; done; or a cycle edge
                    on_path.add(p)
                    stack.append((p, 0))
                    continue
                acc: Set[str] = set()
                for p in ps:
                    if p in want:
                        acc.add(p)
                    else:
                        acc |= nearest.get(p, frozenset())
                acc.discard(oid)
                nearest[oid] = frozenset(acc)
                on_path.discard(oid)
                stack.pop()

        indeg: Dict[str, int] = {oid: 0 for oid in want}
        children: Dict[str, List[str]] = {oid: [] for oid in want}
        for oid in sorted(want):
            for p in sorted(nearest.get(oid, frozenset())):
                indeg[oid] += 1
                children[p].append(oid)
        ready = sorted(oid for oid, d in indeg.items() if d == 0)
        out: List[EditOp] = []
        while ready:
            oid = ready.pop(0)
            out.append(self._ops[oid])
            for c in children[oid]:
                indeg[c] -= 1
                if indeg[c] == 0:
                    ready.append(c)
        done = {op.op_id for op in out}
        for oid in sorted(want):
            if oid not in done:
                out.append(self._ops[oid])
        return out

    def export(self, dest_path: Path) -> int:
        """Write the full log to ``dest_path`` (for backup / shipping)."""
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "w", encoding="utf-8") as f:
            f.write(_HEADER + "\n")
            for op in self.all_ops():
                f.write(json.dumps(op.to_dict()) + "\n")
        return len(self._ops)

    def import_log(self, src_path: Path) -> int:
        """Load ops from an exported log; returns count imported."""
        count = 0
        with open(src_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                self.append(EditOp.from_dict(json.loads(line)))
                count += 1
        return count
