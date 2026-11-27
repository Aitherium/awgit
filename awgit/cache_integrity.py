"""Integrity of git's DERIVED caches: the multi-pack-index and the commit-graph.

Why this exists. On a repo that lives on a 9p/NTFS mount (1,397 packs, a 24 MB
multi-pack-index) git printed ``error: improper chunk offset(s) 16c7ac0 and
176d168``: the multi-pack-index's table of contents did not match the bytes git
read. Next to it sat 34 ``tmp_pack_*``/``tmp_idx_*`` leftovers (806 MB) from killed
fetches, a ``gc.pid`` from the day before, and several UNLOCKED writers of those
caches (peers running ``git multi-pack-index write`` by hand, advisory tools
suggesting ``git commit-graph write``). Two writers on one cache is how it breaks.

The MIDX and the commit-graph are pure, regenerable caches: git falls back to the
per-pack ``.idx`` files without them. So moving a bad one ASIDE is always safe and
never loses an object. This module judges them and does exactly that, and nothing
else: it never touches ``*.pack``, ``*.idx``, ``*.rev``, refs or the object store.

Same principles as ``config_integrity`` (read it first):

* discovery and judging read FILES, never git -- the repo being judged may be the
  broken one, and a preflight that shells out to git costs more than the check;
* every mutation happens under one O_EXCL lock (``<git-dir>/awgit-cache.lock``,
  stale after 600 s) and leaves a JSON ledger row;
* exit 0 ok / 1 problem / 2 could not judge -- never 0 on silence.

Honest limit: a check of the bytes on disk can still be fooled on 9p, where the
page cache may serve stale pages for a file another process just rewrote. A clean
preflight is therefore evidence, not proof, which is why ``cache_guard`` also has a
retry-on-error path for ``AWGIT_CACHE_GUARD=heal``.

Chunk-file layout (git's ``chunk-format``), identical for both caches::

    header      MIDX: 12 bytes  sig(4) version(1) oid_version(1) num_chunks(1) base(1) packs(4)
                CGPH:  8 bytes  sig(4) version(1) hash_version(1) num_chunks(1) base(1)
    TOC         (num_chunks + 1) x [ id(4) offset(8, big-endian) ]
    chunks      ...
    trailer     checksum of everything before it (20 bytes sha1 / 32 bytes sha256)

The last TOC entry has id 0 and its offset is where the final chunk ENDS, which
must be exactly ``file_size - hash_len``. Offsets never decrease and never pass
that point. ``quick`` verifies header + TOC + size (a few small reads, O(1) in
file size); ``deep`` also re-hashes the file against the trailer.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from awgit.config_integrity import STALE_LOCK_S, _fsync_dir, common_dir_of

logger = logging.getLogger(__name__)

OK, ABSENT, BAD, UNREADABLE = "ok", "absent", "bad", "unreadable"

MIDX, COMMIT_GRAPH = "midx", "commit-graph"
LOCK_NAME = "awgit-cache.lock"
LEDGER_ENV = "AWGIT_CACHE_LEDGER"
KEEP_BAD = 2                    # moved-aside copies kept per cache file
TMP_MIN_AGE_H = 6.0             # tmp_* older than this AND not held open may go
TMP_FALLBACK_AGE_H = 24.0       # ...when "held open" cannot be judged
GC_PID_MAX_AGE_H = 12.0         # git's own staleness rule for gc.pid
PACK_WARN = 50                  # git's auto-gc threshold
PACK_WARN_ENV = "AWGIT_PACK_WARN"
TMP_PREFIXES = ("tmp_pack_", "tmp_idx_", "tmp_rev_")

#: id byte of the hash -> (hashlib name, digest length). MIDX calls it oid_version,
#: commit-graph calls it hash_version; the numbering is the same.
_HASHES = {1: ("sha1", 20), 2: ("sha256", 32)}
#: kind -> (signature, header length, supported versions)
_FORMATS = {MIDX: (b"MIDX", 12), COMMIT_GRAPH: (b"CGPH", 8)}
_TOC_ENTRY = 12
_HEX = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


# ── small helpers ─────────────────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _utc_stamp(ts: Optional[float] = None) -> str:
    return _dt.datetime.fromtimestamp(_now() if ts is None else ts, _dt.timezone.utc) \
        .strftime("%Y%m%dT%H%M%SZ")


def _iso(ts: Optional[float] = None) -> str:
    return _dt.datetime.fromtimestamp(_now() if ts is None else ts, _dt.timezone.utc) \
        .isoformat(timespec="seconds")


def _verdict(kind: str, path: Path, state: str, why: str = "", **extra: Any) -> Dict[str, Any]:
    return {"kind": kind, "path": str(path), "state": state, "why": why, **extra}


# ── discovery (files only) ────────────────────────────────────────────────────

def resolve_git_dir(raw: Optional[str] = None) -> Optional[Path]:
    """The shared git dir for ``raw`` (a checkout, a subdir of one, or a git dir).

    Reads ``.git`` / ``gitdir:`` / ``commondir`` files; never runs git. ``$GIT_DIR``
    is honoured when no path is given, because that is what git itself would use.
    """
    if raw is None:
        env = os.environ.get("GIT_DIR", "").strip()
        if env:
            raw = env
    here = Path(raw or os.getcwd())
    if not here.exists():
        return None   # an explicit path that is not there must not resolve to its parent repo
    try:
        here = here.resolve()
    except OSError:
        return None
    if (here / "HEAD").is_file() and (here / "objects").is_dir():
        return here
    for p in (here, *here.parents):
        c = common_dir_of(p)
        if c is not None:
            return c
    return None


def pack_dir(git_dir: Path) -> Path:
    return Path(git_dir) / "objects" / "pack"


def midx_path(git_dir: Path) -> Path:
    return pack_dir(git_dir) / "multi-pack-index"


def commit_graph_paths(git_dir: Path) -> Tuple[List[Path], List[Dict[str, Any]]]:
    """Every commit-graph file git would read, plus verdicts for a broken chain.

    ``objects/info/commit-graph`` and each ``graph-<hash>.graph`` named by
    ``commit-graphs/commit-graph-chain``. A chain line that is not a hash, or names
    a file that is gone, is a problem of its own: git then refuses the whole chain.
    """
    info = Path(git_dir) / "objects" / "info"
    files: List[Path] = [info / "commit-graph"]
    extra: List[Dict[str, Any]] = []
    chain = info / "commit-graphs" / "commit-graph-chain"
    try:
        text = chain.read_text(encoding="ascii", errors="replace")
    except FileNotFoundError:
        return files, extra
    except OSError as exc:
        extra.append(_verdict(COMMIT_GRAPH, chain, UNREADABLE, f"{type(exc).__name__}: {exc}"))
        return files, extra
    for line in text.split():
        if not _HEX.match(line):
            extra.append(_verdict(COMMIT_GRAPH, chain, BAD, f"chain line {line[:20]!r} is not a hash"))
            continue
        f = chain.parent / f"graph-{line}.graph"
        files.append(f)
    return files, extra


# ── judging one chunk file ────────────────────────────────────────────────────

def check_chunk_file(path: Path, kind: str, *, deep: bool = False) -> Dict[str, Any]:
    """Verdict for one MIDX / commit-graph. ``absent`` is fine: git needs neither."""
    sig, hdr_len = _FORMATS[kind]
    path = Path(path)
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except FileNotFoundError:
        return _verdict(kind, path, ABSENT, "not present")
    except OSError as exc:
        return _verdict(kind, path, UNREADABLE, f"{type(exc).__name__}: {exc}")
    try:
        with os.fdopen(fd, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            return _judge(fh, size, path, kind, sig, hdr_len, deep)
    except OSError as exc:
        return _verdict(kind, path, UNREADABLE, f"{type(exc).__name__}: {exc}")


def _judge(fh: Any, size: int, path: Path, kind: str, sig: bytes, hdr_len: int,
           deep: bool) -> Dict[str, Any]:
    def bad(why: str) -> Dict[str, Any]:
        return _verdict(kind, path, BAD, why, size=size)

    if size < hdr_len:
        return bad(f"truncated: {size} bytes, header alone is {hdr_len}")
    head = fh.read(hdr_len)
    if len(head) < hdr_len:
        return bad(f"short read: wanted {hdr_len} header bytes, got {len(head)}")
    if head[:4] != sig:
        return bad(f"wrong signature {head[:4]!r}, want {sig!r}")
    version, hash_id, nchunks = head[4], head[5], head[6]
    if version != 1:
        return bad(f"unsupported version {version}")
    if hash_id not in _HASHES:
        return bad(f"unknown hash id {hash_id} (1=sha1, 2=sha256)")
    algo, hlen = _HASHES[hash_id]
    if nchunks == 0:
        return bad("num_chunks is 0")
    toc_len = (nchunks + 1) * _TOC_ENTRY
    toc_end = hdr_len + toc_len
    end_of_data = size - hlen
    if end_of_data < toc_end:
        return bad(f"truncated: {size} bytes cannot hold the header, a {nchunks + 1}-entry TOC "
                   f"and a {hlen}-byte trailer ({toc_end + hlen} needed)")
    toc = fh.read(toc_len)
    if len(toc) < toc_len:
        return bad(f"short read: wanted {toc_len} TOC bytes, got {len(toc)}")
    prev = toc_end
    for i in range(nchunks + 1):
        cid, off = struct.unpack(">4sQ", toc[i * _TOC_ENTRY:(i + 1) * _TOC_ENTRY])
        last = i == nchunks
        if last and cid != b"\0\0\0\0":
            return bad(f"TOC terminator id is {cid!r}, not 0")
        if not last and cid == b"\0\0\0\0":
            return bad(f"TOC entry {i} has id 0 before the terminator")
        if off < prev:
            return bad(f"improper chunk offset {off:#x} at TOC entry {i} (previous {prev:#x}; "
                       f"offsets must not decrease and start after the TOC at {toc_end:#x})")
        if off > end_of_data:
            return bad(f"improper chunk offset {off:#x} at TOC entry {i} runs past "
                       f"{end_of_data:#x} = file size {size:#x} - {hlen}-byte trailer")
        prev = off
    if prev != end_of_data:
        return bad(f"TOC terminator offset {prev:#x} != {end_of_data:#x} "
                   f"(file size {size:#x} - {hlen}-byte trailer): file is truncated or has extra bytes")
    if deep:
        fh.seek(0)
        h = hashlib.new(algo)
        left = end_of_data
        while left > 0:
            blk = fh.read(min(left, 1 << 20))
            if not blk:
                return bad(f"short read while hashing: {left} bytes missing")
            h.update(blk)
            left -= len(blk)
        trailer = fh.read(hlen)
        if trailer != h.digest():
            return bad(f"{algo} trailer {trailer.hex()[:16]}... != computed {h.hexdigest()[:16]}...")
    return _verdict(kind, path, OK, "", size=size, mode="deep" if deep else "quick",
                    hash=algo, chunks=nchunks)


def check_caches(git_dir: Path, *, deep: bool = False) -> List[Dict[str, Any]]:
    """Verdicts for the MIDX and every commit-graph file. Pure file reads."""
    out = [check_chunk_file(midx_path(git_dir), MIDX, deep=deep)]
    files, extra = commit_graph_paths(git_dir)
    out += extra
    for f in files:
        v = check_chunk_file(f, COMMIT_GRAPH, deep=deep)
        # a chain member that is listed but gone is not "absent": git will refuse the chain
        if v["state"] == ABSENT and f.name.startswith("graph-"):
            v = _verdict(COMMIT_GRAPH, f, BAD, "named by commit-graph-chain but missing")
        out.append(v)
    return out


# ── garbage: tmp packs, gc.pid, pack count ────────────────────────────────────

def open_paths(proc: str = "/proc") -> Tuple[Set[str], bool, str]:
    """(paths held open by any process, complete?, note) from ``/proc/*/fd``.

    ``complete`` is False when /proc cannot be listed or some process's fd table was
    not readable (another user's, without root). Then "nobody holds this open" is a
    guess and the caller must use the longer fallback age. Even complete, it only
    sees THIS host's Linux processes: a Windows git holding a handle on the NTFS
    side is invisible here (NTFS then refuses to delete the file anyway).
    """
    held: Set[str] = set()
    try:
        pids = [d for d in os.listdir(proc) if d.isdigit()]
    except OSError as exc:
        return held, False, f"cannot list {proc}: {type(exc).__name__}"
    denied = 0
    for pid in pids:
        fddir = os.path.join(proc, pid, "fd")
        try:
            names = os.listdir(fddir)
        except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
            continue  # the process exited while we scanned
        except OSError:
            denied += 1
            continue
        for n in names:
            try:
                target = os.readlink(os.path.join(fddir, n))
            except OSError:
                continue
            if target.endswith(" (deleted)"):
                target = target[:-len(" (deleted)")]
            held.add(target)
    if denied:
        return held, False, f"{denied} process fd table(s) unreadable"
    return held, True, ""


def tmp_garbage(git_dir: Path, *, now: Optional[float] = None,
                min_age_h: float = TMP_MIN_AGE_H, fallback_age_h: float = TMP_FALLBACK_AGE_H,
                held: Optional[Tuple[Set[str], bool, str]] = None) -> Dict[str, Any]:
    """``tmp_pack_*``/``tmp_idx_*``/``tmp_rev_*`` in objects/pack and who may go.

    A file QUALIFIES when it is older than ``min_age_h`` and not held open. If open
    handles cannot be fully judged, it must be older than ``fallback_age_h``
    instead, and the result says so. Fresh files are a fetch in flight: never listed
    as removable.
    """
    now = _now() if now is None else now
    pdir = pack_dir(git_dir)
    held_set, complete, note = held if held is not None else open_paths()
    try:
        real_dir = os.path.realpath(str(pdir))
    except OSError:
        real_dir = str(pdir)
    files: List[Dict[str, Any]] = []
    try:
        entries = [e for e in os.scandir(pdir) if e.name.startswith(TMP_PREFIXES)]
    except FileNotFoundError:
        entries = []
    except OSError as exc:
        return {"files": [], "bytes": 0, "qualifying": 0, "qualifying_bytes": 0,
                "judged_open": False, "note": f"cannot list {pdir}: {exc}", "unreadable": True}
    for e in sorted(entries, key=lambda x: x.name):
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if not e.is_file(follow_symlinks=False):
            continue
        age_s = max(0.0, now - st.st_mtime)
        is_open = os.path.join(real_dir, e.name) in held_set
        need_h = min_age_h if complete else max(min_age_h, fallback_age_h)
        qualifies = (not is_open) and age_s >= need_h * 3600.0
        if is_open:
            why = "held open by a running process"
        elif age_s < need_h * 3600.0:
            why = f"younger than {need_h:g}h" + ("" if complete else " (open handles unjudged: fallback age)")
        else:
            why = f"older than {need_h:g}h, not held open"
        files.append({"path": str(Path(pdir) / e.name), "name": e.name, "size": st.st_size,
                      "age_s": round(age_s), "open": is_open, "qualifies": qualifies, "why": why})
    q = [f for f in files if f["qualifies"]]
    return {"files": files, "bytes": sum(f["size"] for f in files), "qualifying": len(q),
            "qualifying_bytes": sum(f["size"] for f in q), "judged_open": complete,
            "note": ("" if complete else f"{note}; using the {fallback_age_h:g}h fallback age")}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, owned by someone else
    except OSError:
        return True   # cannot tell: treat as alive, never delete on a guess
    return True


def gc_pid_state(git_dir: Path, *, now: Optional[float] = None,
                 host: Optional[str] = None, max_age_h: float = GC_PID_MAX_AGE_H,
                 pid_alive: Any = _pid_alive) -> Dict[str, Any]:
    """State of ``<git-dir>/gc.pid``: stale when older than 12h, or its pid is dead
    on THIS host (git's own rule). A pid on another host that is under 12h old is
    left alone: we cannot see that machine's processes."""
    now = _now() if now is None else now
    p = Path(git_dir) / "gc.pid"
    try:
        st = p.stat()
        text = p.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return {"path": str(p), "present": False, "stale": False, "why": "no gc.pid"}
    except OSError as exc:
        return {"path": str(p), "present": True, "stale": False, "unjudged": True,
                "why": f"cannot read gc.pid: {type(exc).__name__}"}
    age_s = max(0.0, now - st.st_mtime)
    parts = text.split(None, 1)
    pid = int(parts[0]) if parts and parts[0].isdigit() else None
    owner = parts[1].strip() if len(parts) > 1 else ""
    me = socket.gethostname() if host is None else host
    row = {"path": str(p), "present": True, "age_s": round(age_s), "pid": pid, "host": owner}
    if age_s >= max_age_h * 3600.0:
        return {**row, "stale": True, "why": f"older than {max_age_h:g}h"}
    if pid is not None and owner == me and not pid_alive(pid):
        return {**row, "stale": True, "why": f"pid {pid} is not running on {me}"}
    return {**row, "stale": False, "why": "gc.pid belongs to a live or foreign process, or is recent"}


def pack_count(git_dir: Path) -> Optional[int]:
    try:
        return sum(1 for e in os.scandir(pack_dir(git_dir)) if e.name.endswith(".pack"))
    except OSError:
        return None


def pack_warn_threshold() -> int:
    try:
        return int(os.environ.get(PACK_WARN_ENV, "") or PACK_WARN)
    except ValueError:
        return PACK_WARN


# ── the whole picture ─────────────────────────────────────────────────────────

def scan(git_dir: Path, *, deep: bool = False, now: Optional[float] = None,
         held: Optional[Tuple[Set[str], bool, str]] = None) -> Dict[str, Any]:
    """One read-only report. ``exit``: 0 ok, 1 problem, 2 could not judge.

    Problems: a bad MIDX/commit-graph, tmp garbage that qualifies for removal, a
    stale gc.pid. Warnings (never change the exit): pack count over threshold.
    """
    git_dir = Path(git_dir)
    caches = check_caches(git_dir, deep=deep)
    garbage = tmp_garbage(git_dir, now=now, held=held)
    gcp = gc_pid_state(git_dir, now=now)
    n = pack_count(git_dir)
    limit = pack_warn_threshold()
    problems: List[str] = []
    unjudged: List[str] = []
    warnings: List[str] = []
    for v in caches:
        if v["state"] == BAD:
            problems.append(f"{v['kind']} {v['path']}: {v['why']}")
        elif v["state"] == UNREADABLE:
            unjudged.append(f"{v['kind']} {v['path']}: {v['why']}")
    if garbage.get("unreadable"):
        unjudged.append(garbage["note"])
    if garbage["qualifying"]:
        problems.append(f"{garbage['qualifying']} stale tmp_* file(s), "
                        f"{garbage['qualifying_bytes']} bytes, in objects/pack")
    if gcp.get("stale"):
        problems.append(f"stale gc.pid ({gcp['why']})")
    if gcp.get("unjudged"):
        unjudged.append(gcp["why"])
    if n is None:
        unjudged.append(f"cannot list {pack_dir(git_dir)}")
    elif n > limit:
        warnings.append(f"{n} packs (threshold {limit}): every object lookup scans them; "
                        f"`awgit maintenance --apply --repack` consolidates")
    if garbage.get("note"):
        warnings.append(garbage["note"])
    code = 1 if problems else (2 if unjudged else 0)
    return {"git_dir": str(git_dir), "mode": "deep" if deep else "quick", "caches": caches,
            "garbage": garbage, "gc_pid": gcp, "packs": {"count": n, "threshold": limit},
            "problems": problems, "unjudged": unjudged, "warnings": warnings, "exit": code}


# ── the lock ──────────────────────────────────────────────────────────────────

class CacheLock:
    """``<git-dir>/awgit-cache.lock``, taken with O_EXCL.

    A lock older than STALE_LOCK_S belongs to a process that died, and is broken
    (renamed away first, so two breakers cannot both win). A long holder -- a
    repack can run for hours -- keeps its lock fresh with a heartbeat, otherwise a
    second awgit would reasonably decide the first one had died.
    """

    def __init__(self, git_dir: Path, owner: str = "awgit") -> None:
        self.path = Path(git_dir) / LOCK_NAME
        self.owner = owner
        self.held = False
        self.why = ""
        self._stop = threading.Event()
        self._beat: Optional[threading.Thread] = None

    def acquire(self, now: Optional[float] = None) -> bool:
        now = _now() if now is None else now
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        for attempt in (1, 2):
            try:
                fd = os.open(str(self.path), flags, 0o644)
                break
            except FileExistsError:
                try:
                    age = now - self.path.stat().st_mtime
                    holder = self.path.read_text(encoding="utf-8", errors="replace").strip()[:200]
                except OSError:
                    continue  # vanished between open and stat: try once more
                if age < STALE_LOCK_S or attempt == 2:
                    self.why = (f"{self.path} is held ({age:.0f}s old; {holder or 'holder unknown'}): "
                                f"another awgit is changing this repo's caches")
                    return False
                grave = self.path.with_name(f"{LOCK_NAME}.stale-{os.getpid()}")
                try:
                    os.replace(str(self.path), str(grave))
                    os.unlink(grave)
                except OSError as exc:
                    self.why = f"stale {self.path} ({age:.0f}s) could not be removed: {exc}"
                    return False
            except OSError as exc:
                self.why = f"cannot create {self.path}: {exc}"
                return False
        else:  # pragma: no cover - both attempts raced
            self.why = f"{self.path} could not be taken"
            return False
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                                     "owner": self.owner, "ts": _iso()}).encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            logger.debug("lock body not written: %s", exc)
        self.held = True
        self._stop.clear()
        self._beat = threading.Thread(target=self._heartbeat, daemon=True)
        self._beat.start()
        return True

    def _heartbeat(self) -> None:
        while not self._stop.wait(STALE_LOCK_S / 6.0):
            try:
                os.utime(self.path, None)
            except OSError:
                return

    def release(self) -> None:
        if not self.held:
            return
        self._stop.set()
        self.held = False
        try:
            os.unlink(self.path)
        except OSError as exc:
            logger.debug("lock already gone: %s", exc)

    def __enter__(self) -> "CacheLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


# ── ledger ────────────────────────────────────────────────────────────────────

def ledger_path() -> Path:
    raw = os.environ.get(LEDGER_ENV, "").strip()
    return Path(raw) if raw else Path.home() / ".aither" / "awgit" / "cache-integrity.jsonl"


def record(row: Dict[str, Any]) -> None:
    """Append one JSON row (fsynced). Never raises: the action already happened."""
    ev = dict(row, ts=_iso(), host=socket.gethostname(), pid=os.getpid())
    try:
        lp = ledger_path()
        lp.parent.mkdir(parents=True, exist_ok=True)
        with open(lp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        logger.debug("ledger row not written: %s", exc)


# ── heal: move a bad derived cache aside ──────────────────────────────────────

def _is_derived_cache(git_dir: Path, path: Path) -> bool:
    """The ONLY files heal may move. Anything else -- packs, idx, rev, refs -- no."""
    try:
        rel = Path(os.path.abspath(path)).relative_to(os.path.abspath(git_dir)).as_posix()
    except ValueError:
        return False
    return (rel == "objects/pack/multi-pack-index"
            or rel == "objects/info/commit-graph"
            or rel == "objects/info/commit-graphs/commit-graph-chain"
            or re.fullmatch(r"objects/info/commit-graphs/graph-[0-9a-f]{40,64}\.graph", rel) is not None)


def _move_aside(path: Path, stamp: str) -> Path:
    dst = path.with_name(f"{path.name}.bad-{stamp}")
    n = 1
    while dst.exists():
        n += 1
        dst = path.with_name(f"{path.name}.bad-{stamp}-{n}")
    os.replace(str(path), str(dst))
    return dst


def _prune_bad(path: Path, keep: int) -> List[str]:
    """Keep the newest ``keep`` ``<name>.bad-*`` beside ``path``; drop older ones."""
    try:
        old = sorted(p for p in path.parent.iterdir() if p.name.startswith(path.name + ".bad-"))
    except OSError:
        return []
    gone: List[str] = []
    for p in old[:-keep] if keep > 0 else old:
        try:
            os.unlink(p)
            gone.append(str(p))
        except OSError as exc:
            logger.debug("old .bad copy not removed: %s", exc)
    return gone


def heal(git_dir: Path, *, force: Iterable[str] = (), keep: int = KEEP_BAD,
         trigger: str = "awgit caches --fix", lock: Optional[CacheLock] = None,
         now: Optional[float] = None) -> Dict[str, Any]:
    """Move a bad MIDX / commit-graph aside as ``<name>.bad-<UTC ts>``.

    Re-judges under the lock (the verdict a caller holds may be stale), and moves
    only what is bad NOW. ``force`` names kinds (``midx``, ``commit-graph``) to move
    even when the bytes pass: used by the guard's retry path, where git itself said
    the cache is wrong and a stale 9p page cache may have hidden it from us. Moving
    a good cache aside costs speed, never data.

    Refuses (``ok`` False, ``refused`` says why) when the lock is held. Pass an
    already-held ``lock`` when the caller owns it (maintenance).
    """
    git_dir = Path(git_dir)
    own = lock is None
    lk = lock or CacheLock(git_dir, owner=trigger)
    if own and not lk.acquire(now):
        return {"ok": False, "refused": lk.why, "moved": [], "pruned": []}
    try:
        forced = set(force)
        stamp = _utc_stamp()
        moved: List[Dict[str, str]] = []
        pruned: List[str] = []
        verdicts = check_caches(git_dir)
        chain_bad = any(v["kind"] == COMMIT_GRAPH and v["state"] == BAD
                        and (Path(v["path"]).name.startswith("graph-")
                             or Path(v["path"]).name == "commit-graph-chain") for v in verdicts)
        targets: List[Tuple[Path, str]] = []
        for v in verdicts:
            if v["state"] == BAD or (v["kind"] in forced and v["state"] != ABSENT):
                targets.append((Path(v["path"]), v["why"] or f"forced ({trigger})"))
        if chain_bad or COMMIT_GRAPH in forced:
            # a split chain with one bad link is unusable as a whole: set the chain
            # file (the index of the links) aside too, so git ignores all of it
            chain = git_dir / "objects" / "info" / "commit-graphs" / "commit-graph-chain"
            if chain.exists() and all(chain != t[0] for t in targets):
                targets.append((chain, "chain contains a bad or forced link"))
            if COMMIT_GRAPH in forced:
                for f in commit_graph_paths(git_dir)[0]:
                    if f.exists() and all(f != t[0] for t in targets):
                        targets.append((f, f"forced ({trigger})"))
        seen = set()
        for path, why in targets:
            if path in seen:
                continue
            seen.add(path)
            if not _is_derived_cache(git_dir, path):
                logger.warning("heal: refusing to move non-cache file %s", path)
                continue
            try:
                dst = _move_aside(path, stamp)
            except FileNotFoundError:
                continue
            except OSError as exc:
                moved.append({"from": str(path), "error": f"{type(exc).__name__}: {exc}", "why": why})
                continue
            _fsync_dir(path.parent)
            moved.append({"from": str(path), "to": str(dst), "why": why})
            pruned += _prune_bad(path, keep)
        errors = [m for m in moved if "error" in m]
        res = {"ok": not errors, "refused": "", "moved": moved, "pruned": pruned}
        if moved or pruned:
            record({"action": "heal", "git_dir": str(git_dir), "trigger": trigger,
                    "moved": moved, "pruned": pruned, "forced": sorted(forced)})
        return res
    finally:
        if own:
            lk.release()


# ── CLI: awgit caches ─────────────────────────────────────────────────────────

def add_parser(sub: Any) -> None:
    p = sub.add_parser("caches", help="check git's derived caches (multi-pack-index, "
                                      "commit-graph), stale tmp packs and gc.pid")
    p.add_argument("--repo", default=None, help="a checkout or git dir (default: current directory)")
    p.add_argument("--deep", action="store_true", help="also verify each file's checksum trailer")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.add_argument("--fix", action="store_true",
                   help="move a bad multi-pack-index/commit-graph aside (regenerable; never "
                        "touches packs). Tmp garbage and gc.pid are `awgit maintenance`.")


def _fmt_age(s: Any) -> str:
    s = int(s)
    return f"{s // 86400}d" if s >= 172800 else (f"{s // 3600}h" if s >= 3600 else f"{s // 60}m")


def render(rep: Dict[str, Any]) -> List[str]:
    lines = [f"git dir   {rep['git_dir']}  ({rep['mode']} check)"]
    for v in rep["caches"]:
        if v["state"] == ABSENT:
            continue
        lines.append(f"{v['kind']:<12}{v['state'].upper():<11}{v['path']}" + (f"  {v['why']}" if v["why"] else ""))
    if not any(v["state"] != ABSENT for v in rep["caches"]):
        lines.append("caches    none present (git uses the per-pack indexes)")
    g = rep["garbage"]
    lines.append(f"tmp packs {len(g['files'])} file(s), {g['bytes']} bytes; {g['qualifying']} removable "
                 f"({g['qualifying_bytes']} bytes)" + ("" if g["judged_open"] else "  [open handles unjudged]"))
    gp = rep["gc_pid"]
    lines.append("gc.pid    " + (("STALE: " if gp.get("stale") else "live/recent: ") + gp["why"]
                                 if gp.get("present") else "none"))
    lines.append(f"packs     {rep['packs']['count']} (warn above {rep['packs']['threshold']})")
    for w in rep["warnings"]:
        lines.append(f"WARNING   {w}")
    for pr in rep["problems"]:
        lines.append(f"PROBLEM   {pr}")
    for u in rep["unjudged"]:
        lines.append(f"UNJUDGED  {u}")
    if any(v["state"] == BAD for v in rep["caches"]):
        lines.append(f"fix       awgit caches --repo {rep['git_dir']} --fix")
    if rep["garbage"]["qualifying"] or rep["gc_pid"].get("stale"):
        lines.append(f"clean up  awgit maintenance --repo {rep['git_dir']} --apply")
    return lines


def run(args: argparse.Namespace) -> int:
    gd = resolve_git_dir(args.repo)
    if gd is None:
        print(f"awgit caches: no git repo at {args.repo or os.getcwd()}", file=sys.stderr)
        return 2
    rep = scan(gd, deep=args.deep)
    healed: Optional[Dict[str, Any]] = None
    if args.fix:
        if any(v["state"] == BAD for v in rep["caches"]):
            healed = heal(gd, trigger="awgit caches --fix")
            rep = scan(gd, deep=args.deep)
            rep["heal"] = healed
        else:
            rep["heal"] = {"ok": True, "refused": "", "moved": [], "pruned": []}
    if args.as_json:
        print(json.dumps(rep, sort_keys=True))
    else:
        for line in render(rep):
            print(line)
        if healed is not None:
            if healed["refused"]:
                print(f"REFUSED   {healed['refused']}", file=sys.stderr)
            for m in healed["moved"]:
                print(f"MOVED     {m['from']} -> {m.get('to') or 'FAILED: ' + m.get('error', '')}")
    if healed is not None and healed["refused"]:
        return 1
    return int(rep["exit"])


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="awgit caches")
    sub = ap.add_subparsers(dest="cmd")
    add_parser(sub)
    return run(ap.parse_args(["caches", *(sys.argv[1:] if argv is None else argv)]))


if __name__ == "__main__":
    raise SystemExit(main())
