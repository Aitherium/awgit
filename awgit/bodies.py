"""Content-addressed body store + disk dedupe index (M6).

The op-log records node bodies by their git-blob SHA — a content address — but
until this module the bytes themselves lived ONLY inside git objects: on this
box, the failing D: drive's ``.git/objects``. ``BodyStore`` materializes them
into ``<data_root>/bodies/<sha[:2]>/<sha>`` so any op can reconstruct a node
body WITHOUT git. Content addressing IS dedupe: identical bodies (across
commits, branches, worktrees, the D:/C:/staging copies) collapse to one blob.

Also provides the disk-level dedupe index: ``scan_tree`` hashes files and
reports identical-content groups — the "things get crazy with all the worktrees
on disk" problem, quantified. ``wasted_bytes`` is what a within-filesystem
hard-link would reclaim; the report is the Phase 6+ stepping stone to actually
reclaiming it.

Drive-failure resilience: unreadable files (0xC0000006 / ENODEV on this box's
failing D:) are skipped, never fatal — the same rule as ``vcs_replay_history``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from awgit.data_root import vcs_data_root
from awgit.oplog import FileLock

logger = logging.getLogger(__name__)


def blob_sha(content: bytes) -> str:
    """Git-blob content address — matches ``git hash-object`` byte-for-byte."""
    return hashlib.sha1(
        b"blob " + str(len(content)).encode() + b"\0" + content
    ).hexdigest()


class BodyStore:
    """Content-addressed store of node bodies (deduped by sha).

    ``put`` is idempotent: storing a body already present is a no-op (the same
    content address returns the same sha and writes nothing). Writes are
    atomic (temp file + ``os.replace``) and fsync'd, serialized by a per-store
    lock so concurrent post-commit captures cannot tear a blob.
    """

    def __init__(self, data_root: Optional[Path] = None) -> None:
        self.data_root = data_root or vcs_data_root()
        self.root = self.data_root / "bodies"

    def _path(self, sha: str) -> Path:
        return self.root / sha[:2] / sha

    def put(self, content: bytes) -> str:
        """Store ``content``, returning its content address. Deduped."""
        sha = blob_sha(content)
        if self._path(sha).exists():
            return sha
        with FileLock(self.data_root / "bodies.lock"):
            path = self._path(sha)
            if path.exists():
                return sha  # another writer beat us to it
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.parent / f".{sha}.tmp"
            with open(tmp, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)  # atomic on POSIX and Windows
        return sha

    def get(self, sha: str) -> Optional[bytes]:
        path = self._path(sha)
        if not path.exists():
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None  # unreadable (failing drive) — caller falls back

    def contains(self, sha: str) -> bool:
        return self._path(sha).exists()

    def stats(self) -> Dict[str, int]:
        if not self.root.exists():
            return {"blobs": 0, "bytes": 0}
        blobs = [p for p in self.root.glob("*/*") if p.is_file()]
        return {
            "blobs": len(blobs),
            "bytes": sum(p.stat().st_size for p in blobs),
        }

    def blob_shas(self) -> set:
        """The set of content addresses currently stored."""
        if not self.root.exists():
            return set()
        return {p.name for p in self.root.glob("*/*") if p.is_file()}

    def gc(self, referenced: set, *, dry_run: bool = True) -> Dict[str, int]:
        """Remove blobs NOT in ``referenced`` (orphans). Never touches a blob
        the op-log references.

        ``referenced`` is the set of body shas the durable op-log names. A blob
        absent from it is an orphan — e.g. a capture process that crashed
        between writing bodies and appending the op. ``dry_run`` (default)
        reports what would be removed without deleting; pass ``dry_run=False``
        to actually reclaim. Shard dirs are pruned when they empty.
        """
        if not self.root.exists():
            return {"removed": 0, "freed": 0}
        candidates = [
            p for p in self.root.glob("*/*")
            if p.is_file() and p.name not in referenced
        ]
        freed = 0
        for p in candidates:
            try:
                freed += p.stat().st_size
            except OSError:
                continue
        if not dry_run:
            for p in candidates:
                try:
                    p.unlink()
                except OSError:
                    continue
            for shard in list(self.root.iterdir()):
                if shard.is_dir() and not any(shard.iterdir()):
                    try:
                        shard.rmdir()
                    except OSError:
                        logger.debug("vcs: could not prune empty shard %s", shard)
        return {"removed": len(candidates), "freed": freed}


def op_referenced_shas(data_root: Optional[Path] = None) -> set:
    """Every body sha the op-log references — the durable truth for GC."""
    from awgit.oplog import OpLog  # lazy: avoid a module-level cycle

    ref: set = set()
    for op in OpLog(data_root=data_root).all_ops():
        for nc in op.node_changes:
            for s in (nc.old_body_sha, nc.new_body_sha):
                if s:
                    ref.add(s)
    return ref


# ── disk dedupe index ─────────────────────────────────────────────────────

def scan_tree(
    root: Path, include: Tuple[str, ...] = ()
) -> Dict[str, List[str]]:
    """Hash every file under ``root`` → ``{sha: [paths]}``.

    ``include`` filters by suffix (e.g. ``(".py",)``); empty = ALL files. The
    disk-dedupe purpose is all files (the space hogs are weights/artifacts, not
    code) — ``include`` is for callers scoping to a language. Files sharing a
    sha are byte-identical — the dedupe index. Unreadable files are skipped
    (failing drive), never fatal.
    """
    index: Dict[str, List[str]] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if ".git" in path.parts:
            continue  # never hash/link git internals (worktrees, submodules)
        if include and path.suffix not in include:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        index.setdefault(blob_sha(data), []).append(str(path))
    return index


def dedupe_report(paths: List[Path]) -> Dict[str, int]:
    """Scan the given trees and report identical-content duplication.

    Returns ``{groups, duplicate_files, wasted_bytes}`` — ``wasted_bytes`` is
    the size of all but the first copy of each group (what a within-filesystem
    hard-link / dedup would reclaim). Files identical across TWO trees (D: and
    C:, say) can't be hard-linked (different filesystems) but the report still
    names them — that is the duplication the owner flagged.
    """
    combined: Dict[str, List[str]] = {}
    for root in paths:
        if not root.exists():
            continue
        for sha, found in scan_tree(root).items():
            combined.setdefault(sha, []).extend(found)
    groups = {sha: p for sha, p in combined.items() if len(p) > 1}
    duplicate_files = sum(len(p) - 1 for p in groups.values())
    wasted = 0
    for _sha, p in groups.items():
        try:
            wasted += (len(p) - 1) * Path(p[0]).stat().st_size
        except OSError:
            continue
    return {"groups": len(groups), "duplicate_files": duplicate_files,
            "wasted_bytes": wasted}


# ── reclaim: actually collapse the duplicates ─────────────────────────────

def _repo_root(path: Path) -> Optional[Path]:
    """Nearest ancestor containing a `.git` entry, if any (bounded walk)."""
    cur = path if path.is_dir() else path.parent
    for _ in range(8):
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _tracked_abs_paths(roots: List[Path]) -> Dict[Path, set]:
    """``{repo_root: {absolute tracked path}}`` — one ``git ls-files -z`` per root."""
    out: Dict[Path, set] = {}
    for root in roots:
        try:
            raw = subprocess.run(
                ["git", "ls-files", "-z"], cwd=str(root),
                capture_output=True, check=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            continue
        rels = raw.split(b"\0")
        out[root] = {
            str((root / r.decode("utf-8", "replace")).resolve())
            for r in rels if r
        }
    return out


def reclaim(paths: List[Path], *, dry_run: bool = True) -> Dict[str, int]:
    """Hard-link byte-identical duplicates to one copy per group.

    This is the "actually reclaim the space" half of the dedupe report. Safety:

      - links ONLY within a filesystem (hard links cannot cross ``st_dev`` — D:
        and C: duplicates are reported by ``dedupe_report`` but never linked);
      - git-TRACKED paths are never linked: an editor writing in place would
        silently diverge two paths sharing an inode (the duplicate stops being
        a copy the moment either is edited);
      - ``dry_run`` (default) reports exactly what would be linked; pass
        ``dry_run=False`` to act.

    Linking is atomic: ``os.link`` to a temp name, then ``os.replace`` over the
    duplicate — no window where the path is missing, and the content lives in
    the canonical inode regardless (deleting canonical later keeps the
    hard-linked copies intact).
    """
    candidates: List[Dict[str, object]] = []
    for root in paths:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if ".git" in path.parts:
                continue  # never hash/link git internals (worktrees, submodules)
            try:
                st = path.stat()
                data = path.read_bytes()
            except OSError:
                continue  # unreadable (failing drive) — skip, never fatal
            candidates.append({
                "path": str(path.resolve()),
                "sha": blob_sha(data),
                "dev": st.st_dev,
                "size": st.st_size,
            })

    repo_roots = {r for c in candidates if (r := _repo_root(Path(c["path"])))}
    tracked = _tracked_abs_paths(list(repo_roots))

    def is_tracked(p: str) -> bool:
        return any(p in tset for tset in tracked.values())

    groups: Dict[Tuple[str, int], List[Dict[str, object]]] = {}
    skipped_tracked = 0
    for c in candidates:
        if is_tracked(str(c["path"])):
            skipped_tracked += 1
            continue
        groups.setdefault((str(c["sha"]), int(c["dev"])), []).append(c)

    linked = 0
    reclaimed = 0
    group_count = 0
    for _key, members in groups.items():
        if len(members) < 2:
            continue
        group_count += 1
        canonical = str(members[0]["path"])
        for dup in members[1:]:
            dup_path = str(dup["path"])
            if not dry_run:
                tmp = dup_path + ".vcs-linktmp"
                try:
                    os.link(canonical, tmp)
                    os.replace(tmp, dup_path)
                except OSError:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        logger.debug("vcs: could not clean up link temp %s", tmp)
                    continue  # failed to link this one; keep going
            linked += 1
            reclaimed += int(dup["size"])
    return {
        "groups": group_count,
        "linked": linked,
        "reclaimed_bytes": reclaimed,
        "skipped_tracked": skipped_tracked,
    }
