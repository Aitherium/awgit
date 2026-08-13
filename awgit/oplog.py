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
from typing import Dict, Iterable, List, Optional

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


class OpLog:
    """Append-only semantic op log backed by one JSONL file."""

    def __init__(self, data_root: Optional[Path] = None) -> None:
        self._data_root = data_root or vcs_data_root()
        self.path = self._data_root / "oplog.jsonl"
        self._lock = RLock()
        self._ops: Dict[str, EditOp] = {}
        self._load()

    # ── persistence ────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            return
        with FileLock(_lock_path(self._data_root)):
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    op = EditOp.from_dict(json.loads(line))
                    self._ops[op.op_id] = op

    def append(self, op: EditOp) -> None:
        """Append one op durably. Idempotent by op_id (callers may retry)."""
        with self._lock:
            if op.op_id in self._ops:
                return
            with FileLock(_lock_path(self._data_root)):
                self._data_root.mkdir(parents=True, exist_ok=True)
                fresh = not self.path.exists()
                with open(self.path, "a", encoding="utf-8") as f:
                    if fresh:
                        f.write(_HEADER + "\n")
                    f.write(json.dumps(op.to_dict()) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            self._ops[op.op_id] = op

    # ── queries ────────────────────────────────────────────────────────

    def all_ops(self) -> List[EditOp]:
        return list(self._ops.values())

    def ops_for_commit(self, git_sha: str) -> List[EditOp]:
        return [op for op in self._ops.values() if op.git_sha == git_sha]

    def ops_for_node(self, node_id: str) -> List[EditOp]:
        return [
            op for op in self._ops.values()
            if any(nc.node_id == node_id for nc in op.node_changes)
        ]

    def ops_since(self, ts: str) -> List[EditOp]:
        return [op for op in self._ops.values() if op.ts >= ts]

    def ops_by(self, actor: str) -> List[EditOp]:
        return [op for op in self._ops.values() if op.actor == actor]

    def sha_index(self) -> Dict[str, str]:
        return {op.git_sha: op.op_id for op in self._ops.values()}

    def has_commit(self, git_sha: str) -> bool:
        return any(op.git_sha == git_sha for op in self._ops.values())

    def get(self, op_id: str) -> Optional[EditOp]:
        return self._ops.get(op_id)

    def linearize(self, op_ids: Iterable[str]) -> List[EditOp]:
        """Order ops so causal parents precede their children (stable)."""
        want = {oid for oid in op_ids if oid in self._ops}
        if not want:
            return []
        indeg: Dict[str, int] = {oid: 0 for oid in want}
        children: Dict[str, List[str]] = {oid: [] for oid in want}
        for oid in want:
            for p in self._ops[oid].parent_ops:
                if p in want:
                    indeg[oid] += 1
                    children.setdefault(p, []).append(oid)
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
