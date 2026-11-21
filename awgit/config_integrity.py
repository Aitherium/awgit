"""Repo integrity: keep every managed repo's ``.git/config`` restorable.

    awgit integrity                 check + snapshot every managed repo (read-mostly)
    awgit integrity --fix           ... and restore a corrupt config from its last good copy
    awgit integrity --json          one JSON row per repo
    awgit integrity --repo PATH     only this repo (repeatable)
    awgit integrity --prune-stale-branches [--apply]
                                    drop ``[branch "x"]`` sections whose branch is gone

WHY (measured 2026-11-13 and 2026-11-14). ``C:\\AitherOS-Fresh\\.git\\config`` was
found ALL NUL BYTES, same size as before (19,719 then 30,949 bytes), twice. Both
times the host had just died uncleanly: 11-13 a hard reboot; 11-14 a bugcheck 0x50
(PAGE_FAULT_IN_NONPAGED_AREA, host commit ~96 %) a few minutes after git rewrote
the config at 13:08:04 -- the hourly reaper enumerated worktrees fine at 13:08:30
(so the bytes were still good in the cache), its log stops there, and the machine
was back up at 13:13. Small files written in the same minutes survived because
NTFS keeps a file under ~700 bytes RESIDENT in its MFT record (journaled metadata);
a 30 KB config is non-resident, its clusters are written later by the lazy writer,
and a crash before that leaves the journaled size with never-written (zero) data.

git writes the config the right way for concurrency -- ``config.lock`` then rename
-- but never fsyncs it: ``core.fsync`` has no config component at all. So the
rename is durable and the bytes are not. Any ``git config`` / ``branch -D`` /
``worktree add -b <x> origin/...`` (upstream tracking) inside the lazy-write window
of a crash can produce this, and a 30 KB file rewritten many times a day makes
that window wide. Nothing in git can be changed from here; what can:

* snapshot every VALID config (keep the newest ``KEEP``) under
  ``~/.aither/git-config-backups/<repo-key>/`` -- written temp + fsync + replace;
* restore the newest valid snapshot when the live file is all-NUL, empty,
  missing or rejected by ``git config --list`` -- under git's OWN lock
  (``config.lock``, O_EXCL), fsynced, then renamed, so a concurrent git waits and
  the restored bytes survive the next crash;
* fsync the live config whenever a new snapshot is taken, which closes the
  window for the copy git left unflushed;
* keep the file small: awgit worktrees are created ``--no-track``, and
  ``--prune-stale-branches`` drops sections for branches that no longer exist.

A restore never synthesizes a config. With no good snapshot it refuses and says
so (exit 1): a guessed config is a different repository.

Discovery reads files, never git: a repo whose config is zeroed makes every git
command fatal ("bad config line 1"), and this is the code that must still run.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob as _glob
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

KEEP = 5
#: a ``config.lock`` older than this belongs to a git that died (the crash case);
#: younger, a git is writing right now and the restore waits for the next tick.
STALE_LOCK_S = 600.0
BACKUPS_ENV = "AWGIT_CONFIG_BACKUPS"
LEDGER_ENV = "AWGIT_INTEGRITY_LEDGER"
REPOS_ENV = "AWGIT_INTEGRITY_REPOS"
DECISION_KEY = "awgit.config_integrity.restore"

OK, MISSING, EMPTY, ZEROED, UNPARSEABLE = "ok", "missing", "empty", "zeroed", "unparseable"
CORRUPT = (MISSING, EMPTY, ZEROED, UNPARSEABLE)


# ── where things live ─────────────────────────────────────────────────────────

def backups_root() -> Path:
    raw = os.environ.get(BACKUPS_ENV, "").strip()
    return Path(raw) if raw else Path.home() / ".aither" / "git-config-backups"


def ledger_path() -> Path:
    raw = os.environ.get(LEDGER_ENV, "").strip()
    return Path(raw) if raw else Path.home() / ".aither" / "awgit" / "config-integrity.jsonl"


def _norm(p: object) -> str:
    return os.path.normcase(os.path.abspath(str(p))).replace("\\", "/").rstrip("/")


def repo_key(common_dir: Path) -> str:
    """Stable, readable directory name for one repo's snapshots."""
    n = _norm(common_dir)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", n).strip("_")[-60:]
    return f"{slug}-{hashlib.sha1(n.encode('utf-8')).hexdigest()[:8]}"


# ── discovery (files only) ────────────────────────────────────────────────────

def common_dir_of(tree: Path) -> Optional[Path]:
    """The shared git dir of a checkout, read from disk. None when not a checkout."""
    dotgit = Path(tree) / ".git"
    try:
        if dotgit.is_dir():
            return dotgit if (dotgit / "HEAD").is_file() else None
        if not dotgit.is_file():
            return None
        line = dotgit.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = Path(line[7:].strip())
    if not gitdir.is_absolute():
        gitdir = (Path(tree) / gitdir).resolve()
    try:
        cd = (gitdir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        return gitdir if (gitdir / "HEAD").is_file() else None
    common = Path(cd) if os.path.isabs(cd) else (gitdir / cd).resolve()
    return common if (common / "HEAD").is_file() else None


def _managed_roots() -> List[Path]:
    """Worktree roots from awgit's settings, expanded; never raises."""
    try:
        from awgit import wtlife
        settings = wtlife.load_settings()
    except Exception:  # noqa: BLE001 - an unreadable settings file is "no roots"
        return []
    out: List[Path] = []
    for raw in settings.get("roots") or []:
        s = str(raw)
        hits = _glob.glob(s) if any(c in s for c in "*?[") else [s]
        out += [Path(h) for h in hits if os.path.isabs(h) and os.path.isdir(h)]
    return out


def discover(extra: Iterable[object] = (), cwd: Optional[Path] = None,
             roots: Optional[Sequence[Path]] = None) -> List[Path]:
    """Every shared git dir awgit manages on this node, deduplicated.

    Sources: the checkout ``cwd`` is in (walking up), ``$AWGIT_INTEGRITY_REPOS``
    (os.pathsep list), ``extra``, and every worktree under awgit's configured roots
    -- whose ``.git`` file leads back to its main repo, so a root that holds
    worktrees of two repos (AitherOS and media-forge) yields both.
    """
    seen: Dict[str, Path] = {}

    def add(common: Optional[Path]) -> None:
        if common is not None:
            seen.setdefault(_norm(common), Path(common))

    here = Path(cwd or os.getcwd()).resolve()
    for p in (here, *here.parents):
        c = common_dir_of(p)
        if c is not None:
            add(c)
            break
    env = os.environ.get(REPOS_ENV, "")
    for raw in [*filter(None, env.split(os.pathsep)), *extra]:
        p = Path(str(raw))
        add(common_dir_of(p) if (p / ".git").exists() else (p if (p / "HEAD").is_file() else None))
    for root in (_managed_roots() if roots is None else roots):
        add(common_dir_of(root))
        try:
            kids = list(os.scandir(root))
        except OSError:
            continue
        for e in kids:
            try:
                if e.is_dir(follow_symlinks=False) and os.path.isfile(os.path.join(e.path, ".git")):
                    add(common_dir_of(Path(e.path)))
            except OSError:
                continue
    return sorted(seen.values(), key=_norm)


# ── judging one file ──────────────────────────────────────────────────────────

def _read(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def git_rejects(path: Path) -> Optional[str]:
    """git's own verdict on a config file: its error text, '' when it parses, None
    when git cannot be run (then nothing is judged unparseable on a guess).

    Runs from a neutral directory with ``--file`` so the repo whose config this is
    is never opened -- that repo may be the one that is broken."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        p = subprocess.run(["git", "config", "--no-includes", "--file", str(path), "--list"],
                           cwd=tempfile.gettempdir(), env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    return "" if p.returncode == 0 else ((p.stderr or p.stdout).strip() or f"rc={p.returncode}")


def classify_bytes(data: Optional[bytes]) -> Tuple[str, str]:
    """(state, why) from content alone. ``unparseable`` here is only the NUL case."""
    if data is None:
        return MISSING, "file is missing"
    if not data:
        return EMPTY, "file is 0 bytes"
    if not data.strip(b"\0"):
        return ZEROED, f"all {len(data)} bytes are NUL (size kept, data never written)"
    if b"\0" in data:
        return UNPARSEABLE, f"{data.count(bytes(1))} NUL byte(s) inside the text"
    if b"[core]" not in data:
        return UNPARSEABLE, "no [core] section"
    return OK, ""


def classify(path: Path, use_git: bool = True) -> Tuple[str, str]:
    state, why = classify_bytes(_read(path))
    if state == OK and use_git:
        err = git_rejects(path)
        if err:
            return UNPARSEABLE, err.splitlines()[0][:200]
    return state, why


# ── durable writes ────────────────────────────────────────────────────────────

def _fsync_dir(d: Path) -> None:
    if os.name == "nt":
        return  # NTFS journals the rename; a directory handle cannot be fsynced here
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        logger.debug("best-effort step skipped: %s", exc)
    finally:
        os.close(fd)


def fsync_file(path: Path) -> bool:
    """Flush a file git wrote but never fsynced. False when it could not."""
    try:
        with open(path, "r+b") as fh:
            os.fsync(fh.fileno())
        return True
    except OSError:
        return False


def write_atomic(path: Path, data: bytes) -> None:
    """temp in the same dir + fsync + replace (+ dir fsync on POSIX)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError as exc:
            logger.debug("best-effort step skipped: %s", exc)
        raise
    _fsync_dir(path.parent)


def write_under_git_lock(cfg: Path, data: bytes, now: Optional[float] = None) -> Tuple[bool, str]:
    """Replace ``cfg`` the way git does (exclusive ``<cfg>.lock``, rename) -- plus the
    fsync git skips. A live lock means a git is writing: refuse, the next tick retries.
    A lock older than STALE_LOCK_S is the leftover of a git that died with the host."""
    lock = cfg.with_name(cfg.name + ".lock")
    now = time.time() if now is None else now
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    for attempt in (1, 2):
        try:
            fd = os.open(str(lock), flags, 0o644)
            break
        except FileExistsError:
            try:
                age = now - lock.stat().st_mtime
            except OSError:
                continue
            if age < STALE_LOCK_S or attempt == 2:
                return False, f"{lock} is held ({age:.0f}s old): a git is writing; retry next tick"
            try:
                os.unlink(lock)
            except OSError as exc:
                return False, f"stale {lock} ({age:.0f}s) could not be removed: {exc}"
    else:  # pragma: no cover - both attempts raced
        return False, f"{lock} could not be taken"
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(lock), str(cfg))
    except OSError as exc:
        try:
            os.unlink(lock)
        except OSError as exc:
            logger.debug("best-effort step skipped: %s", exc)
        return False, f"write failed: {exc}"
    _fsync_dir(cfg.parent)
    return True, "written under config.lock, fsynced"


# ── snapshots ─────────────────────────────────────────────────────────────────

def snapshot_dir(common_dir: Path) -> Path:
    return backups_root() / repo_key(common_dir)


def snapshots(common_dir: Path) -> List[Path]:
    """Newest first."""
    d = snapshot_dir(common_dir)
    try:
        return sorted(d.glob("config-*"), key=lambda p: p.name, reverse=True)
    except OSError:
        return []


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def take_snapshot(common_dir: Path, data: bytes, keep: int = KEEP,
                  now: Optional[float] = None) -> Optional[Path]:
    """Store ``data`` (already judged valid) unless the newest snapshot is the same
    bytes; prune to ``keep``. Returns the new snapshot, None when unchanged."""
    have = snapshots(common_dir)
    sha = _sha(data)
    if have and have[0].name.endswith(f"-{sha}"):
        return None
    ts = _dt.datetime.fromtimestamp(time.time() if now is None else now, _dt.timezone.utc)
    d = snapshot_dir(common_dir)
    snap = d / f"config-{ts.strftime('%Y%m%dT%H%M%S%fZ')}-{sha}"
    write_atomic(snap, data)
    try:
        (d / "SOURCE").write_text(str(common_dir / "config") + "\n", encoding="utf-8")
    except OSError as exc:
        logger.debug("best-effort step skipped: %s", exc)
    for old in snapshots(common_dir)[max(1, keep):]:
        try:
            old.unlink()
        except OSError as exc:
            logger.debug("best-effort step skipped: %s", exc)
    return snap


def newest_good(common_dir: Path, use_git: bool = True) -> Optional[Tuple[Path, bytes]]:
    for snap in snapshots(common_dir):
        data = _read(snap)
        if data is None:
            continue
        st, _ = classify_bytes(data)
        if st == OK and (not use_git or not git_rejects(snap)):
            return snap, data
    return None


# ── one repo ──────────────────────────────────────────────────────────────────

def restore(common_dir: Path, *, use_git: bool = True,
            now: Optional[float] = None) -> Dict[str, object]:
    """Put the newest good snapshot back. The corrupt file is kept beside it as
    ``config.zeroed-<UTC>`` first -- evidence, never deleted here."""
    cfg = common_dir / "config"
    bad = _read(cfg)
    good = newest_good(common_dir, use_git=use_git)
    if good is None:
        return {"restored": False,
                "why": f"no good snapshot under {snapshot_dir(common_dir)}; NOT synthesizing one"}
    snap, data = good
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    kept = None
    if bad is not None:
        kept = common_dir / f"config.zeroed-{stamp}"
        try:
            write_atomic(kept, bad)
        except OSError as exc:
            return {"restored": False, "why": f"could not keep the corrupt copy at {kept}: {exc}"}
    ok, why = write_under_git_lock(cfg, data, now=now)
    if not ok:
        return {"restored": False, "why": why, "kept": str(kept) if kept else None}
    after, after_why = classify(cfg, use_git=use_git)
    age_s = None
    m = re.match(r"config-(\d{8}T\d{6})", snap.name)
    if m:
        t = _dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=_dt.timezone.utc)
        age_s = round((time.time() if now is None else now) - t.timestamp())
    return {"restored": after == OK and _read(cfg) == data, "why": after_why or why,
            "from": str(snap), "snapshot_age_s": age_s, "kept": str(kept) if kept else None,
            "bytes": len(data)}


def worktree_configs(common_dir: Path) -> List[Tuple[Path, str, str]]:
    """Per-worktree ``config.worktree`` files that are corrupt (reported, not
    restored: they hold sparse settings only git can regenerate)."""
    out = []
    for p in sorted((common_dir / "worktrees").glob("*/config.worktree")):
        st, why = classify_bytes(_read(p))
        if st == ZEROED or (st == UNPARSEABLE and b"\0" in (_read(p) or b"")):
            out.append((p, st, why))
    return out


def check_repo(common_dir: Path, *, fix: bool = False, use_git: bool = True,
               keep: int = KEEP, now: Optional[float] = None) -> Dict[str, object]:
    cfg = common_dir / "config"
    state, why = classify(cfg, use_git=use_git)
    row: Dict[str, object] = {"repo": str(common_dir), "config": str(cfg), "state": state,
                              "why": why, "action": "none",
                              "snapshots": len(snapshots(common_dir))}
    if state == OK:
        data = _read(cfg) or b""
        snap = take_snapshot(common_dir, data, keep=keep, now=now)
        if snap is not None:
            row["action"] = "snapshot"
            row["snapshot"] = str(snap)
            row["fsynced"] = fsync_file(cfg)
            row["snapshots"] = len(snapshots(common_dir))
        row["bytes"] = len(data)
        row["branch_sections"] = data.count(b'[branch "')
    elif fix:
        res = restore(common_dir, use_git=use_git, now=now)
        row.update({k: v for k, v in res.items() if k != "why"})
        row["action"] = "restored" if res.get("restored") else "refused"
        row["restore_why"] = res.get("why")
        if res.get("restored"):
            row["state_before"] = state
            row["state"] = OK
    else:
        row["action"] = "needs-fix"
    bad_wt = worktree_configs(common_dir)
    if bad_wt:
        row["worktree_configs_corrupt"] = [f"{p} ({st}: {w})" for p, st, w in bad_wt]
    return row


# ── reporting ─────────────────────────────────────────────────────────────────

def record(row: Dict[str, object]) -> Optional[str]:
    """Append a restore/refusal to the integrity ledger and, when the awdecide brick
    is installed, as a resolved decision (key ``awgit.config_integrity.restore``) with
    its context attached -- so recurrences are counted and the eval sets see them.
    Returns the decision id, or None. Never raises."""
    ev = dict(row, ts=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
              host=_host())
    try:
        lp = ledger_path()
        lp.parent.mkdir(parents=True, exist_ok=True)
        with open(lp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        logger.debug("best-effort step skipped: %s", exc)
    try:
        from awdecide.ledger import Ledger  # optional sibling brick
    except Exception:  # noqa: BLE001
        return None
    did = "gci-" + hashlib.sha1(f"{row.get('repo')}|{ev['ts']}".encode()).hexdigest()[:16]
    try:
        db_env = os.environ.get("AWGIT_DECIDE_DB", "").strip()
        led = Ledger(Path(db_env) if db_env else None)
        try:
            led.ingest(did, key=DECISION_KEY, kind="choice",
                       value=str(row.get("action")), probability=1.0,
                       backend="awgit.integrity",
                       state_sha=hashlib.sha256(str(row.get("state_before") or row.get("state"))
                                                .encode()).hexdigest()[:16],
                       correct=row.get("action") == "restored")
        finally:
            led.close()
        try:
            from awdecide import record as _rec
            _rec.attach(led.path, did, caller="awgit.integrity", rung="rule",
                        state_text=json.dumps({k: row.get(k) for k in (
                            "repo", "state_before", "state", "why", "from",
                            "snapshot_age_s", "bytes", "kept")}, sort_keys=True))
        except Exception as exc:  # noqa: BLE001 - the claim is recorded; context is best-effort
            logger.debug("best-effort step skipped: %s", exc)
    except Exception:  # noqa: BLE001
        return None
    return did


def _host() -> str:
    try:
        import socket
        return socket.gethostname()
    except Exception:  # noqa: BLE001
        return ""


def sweep(repos: Optional[Sequence[Path]] = None, *, fix: bool = False, use_git: bool = True,
          keep: int = KEEP) -> List[Dict[str, object]]:
    rows = []
    for common in (discover() if repos is None else repos):
        row = check_repo(Path(common), fix=fix, use_git=use_git, keep=keep)
        if row["action"] in ("restored", "refused"):
            row["decision_id"] = record(row)
        rows.append(row)
    return rows


def exit_code(rows: Sequence[Dict[str, object]]) -> int:
    if not rows:
        return 2
    return 1 if any(r["state"] != OK or r.get("worktree_configs_corrupt") for r in rows) else 0


# ── keeping the file small ────────────────────────────────────────────────────

_SECTION = re.compile(r'^\s*\[branch\s+"((?:[^"\\]|\\.)*)"\s*\]')


def stale_branch_sections(text: str, exists) -> Tuple[str, List[str]]:
    """(new text, dropped branch names): every ``[branch "x"]`` section whose branch
    ``exists(x)`` says is gone is removed with its keys. Comments and every other
    section are kept byte for byte."""
    out: List[str] = []
    dropped: List[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        m = _SECTION.match(line)
        if m or re.match(r"^\s*\[", line):
            skipping = bool(m) and not exists(m.group(1))
            if skipping:
                dropped.append(m.group(1))
        if not skipping:
            out.append(line)
    return "".join(out), dropped


def prune_stale_branches(common_dir: Path, apply: bool = False) -> Dict[str, object]:
    cfg = common_dir / "config"
    st, why = classify(cfg)
    if st != OK:
        return {"repo": str(common_dir), "pruned": [], "why": f"config is {st}: {why}"}
    p = subprocess.run(["git", "--git-dir", str(common_dir), "for-each-ref",
                        "--format=%(refname:short)",
                        "refs/heads"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if p.returncode != 0:
        return {"repo": str(common_dir), "pruned": [],
                "why": f"for-each-ref failed: {p.stderr.strip()}"}
    branches = set(p.stdout.split())
    data = (_read(cfg) or b"").decode("utf-8")
    new, dropped = stale_branch_sections(data, lambda b: b in branches)
    row: Dict[str, object] = {"repo": str(common_dir), "pruned": dropped, "bytes_before": len(data),
                              "bytes_after": len(new.encode("utf-8")), "applied": False}
    if apply and dropped:
        take_snapshot(common_dir, data.encode("utf-8"))
        ok, why = write_under_git_lock(cfg, new.encode("utf-8"))
        row["applied"], row["why"] = ok, why
    return row


# ── after an unclean boot: everything else a crash may have zeroed ────────────
#
# 2026-11-14 the same bugcheck that zeroed the config zeroed 1,113 tracked files in
# another checkout and a JSON index under ~/.aither: the OS loses every non-resident
# write still in the cache, whoever made it. So after an UNCLEAN boot (Windows:
# Kernel-Power 41 / EventLog 6008 since boot; elsewhere every new boot) the guard
# sweeps once, bounded by a time budget, and resumes next tick if it runs out:
#   * git metadata of every managed repo and worktree (index, HEAD, packed-refs,
#     config.worktree) -- a zeroed index is set aside and rebuilt from HEAD
#     (`git reset -q`, working files untouched; its staged state was already lost);
#     a zeroed HEAD/ref is reported, never guessed;
#   * tracked files in every checkout ACTIVE in the window before the boot (index,
#     HEAD or logs/HEAD touched): all-NUL working copies of non-NUL blobs are put back
#     from git (`git checkout --`);
#   * state files under ~/.aither (json/jsonl/yaml/toml/txt touched in the window):
#     restored from a valid sibling ``.bak`` when there is one, otherwise reported.

SCAN_WINDOW_S = 6 * 3600.0
SCAN_EXTS = (".json", ".jsonl", ".yaml", ".yml", ".toml", ".txt", ".md")
#: bulk stores under ~/.aither with no small state worth a sweep (or rebuilt on demand)
SCAN_SKIP_DIRS = frozenset({"git-config-backups", "__pycache__", "node_modules", ".git",
                            "logs", "agent-trace", "backups", "harness-rooms", "aeon-spool",
                            "cache", ".cache", "tmp",
                            # 2026-11-14: 371,942 files, 56.5 s for the bare walk (#16946)
                            "harvest"})
#: the guard wake (host-wakes.yaml git-config-integrity) is killed at 240 s; the sweep must
#: stop well inside that, and the budget is now checked INSIDE a unit, not only between.
SCAN_BUDGET_S = 120.0
#: crash-scan.json is rewritten after a finished unit at most this often, so a kill keeps
#: every unit finished before it (#16946: the state was saved only on return).
SCAN_SAVE_EVERY_S = 2.0
#: how many files of a unit are judged between two budget checks
SCAN_CHECK_EVERY = 256


def scan_state_path() -> Path:
    return ledger_path().with_name("crash-scan.json")


def boot_time() -> float:
    if os.name == "nt":
        try:
            import ctypes
            up = ctypes.windll.kernel32.GetTickCount64() / 1000.0  # type: ignore[attr-defined]
            return time.time() - up
        except Exception:  # noqa: BLE001
            return 0.0
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError as exc:
        logger.debug("best-effort step skipped: %s", exc)
    return 0.0


def unclean_boot(boot: float) -> Tuple[bool, str]:
    """Did the boot at ``boot`` follow a crash? Windows asks the System log; elsewhere
    there is no cheap honest answer, so every new boot counts (the sweep is bounded)."""
    if os.name != "nt":
        return True, "not Windows: every new boot is swept"
    ms = int(max(0.0, time.time() - boot) * 1000) + 900_000
    q = (f"*[System[(EventID=41 or EventID=6008 or (EventID=1001 and "
         f"Provider[@Name='Microsoft-Windows-WER-SystemErrorReporting'])) and "
         f"TimeCreated[timediff(@SystemTime) <= {ms}]]]")
    try:
        p = subprocess.run(["wevtutil", "qe", "System", f"/q:{q}", "/c:5", "/rd:true", "/f:text"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return True, f"event log unreadable ({exc}); swept to be safe"
    text = p.stdout or ""
    if "Event ID: 1001" in text or "bugcheck" in text.lower():
        m = re.search(r"bugcheck was: (0x[0-9a-fA-F]+)", text)
        return True, f"bugcheck {m.group(1) if m else ''}".strip()
    if "Event ID: 41" in text or "Event ID: 6008" in text:
        return True, "unexpected shutdown (Kernel-Power 41 / 6008)"
    return False, "clean boot"


def _all_nul(path: Path, limit: int = 64 << 20) -> bool:
    try:
        size = path.stat().st_size
        if size == 0 or size > limit:
            return False
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    return True
                if chunk.strip(b"\0"):
                    return False
    except OSError:
        return False


def _checkouts(common_dir: Path) -> List[Tuple[Path, Path]]:
    """(working tree, its private git dir) for the main checkout and every worktree."""
    out: List[Tuple[Path, Path]] = []
    if common_dir.name == ".git":
        out.append((common_dir.parent, common_dir))
    for gd in sorted((common_dir / "worktrees").glob("*")):
        try:
            dotgit = Path((gd / "gitdir").read_text(encoding="utf-8").strip())
        except OSError:
            continue
        if dotgit.parent.is_dir():
            out.append((dotgit.parent, gd))
    return out


def _gitc(tree: Path, *a: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(tree), *a], capture_output=True, timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class _OutOfBudgetError(Exception):
    """The sweep's time budget ran out inside a unit."""


def _merge_found(old: Sequence[Dict[str, str]],
                 new: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    """``old`` + ``new``, one entry per (kind, path), the later verdict winning -- a unit
    cut short is swept again next tick and must not count its hits twice."""
    out: Dict[Tuple[str, str], Dict[str, str]] = {}
    for f in [*old, *new]:
        k = (f.get("kind", ""), f.get("path", ""))
        out.pop(k, None)
        out[k] = f
    return list(out.values())


def zero_scan(repos: Sequence[Path], *, fix: bool, boot: float,
              window_s: float = SCAN_WINDOW_S, budget_s: float = SCAN_BUDGET_S,
              aither_home: Optional[Path] = None, done: Sequence[str] = (),
              progress: Optional[Callable[[Dict[str, object]], None]] = None,
              ) -> Dict[str, object]:
    """One bounded sweep for all-NUL files a crash left behind (see the block above).
    ``done`` names units (checkouts, ~/.aither directories) already swept for this boot;
    the result's ``done`` grows.

    The budget is checked between units AND inside one -- per directory, every
    ``SCAN_CHECK_EVERY`` files, and as the timeout of the read-only ``git ls-files`` -- so
    the sweep returns within about one file operation of ``budget_s`` instead of being
    killed by the wake's timeout with nothing saved (#16946). A unit cut short is not
    ``done``; the next tick starts it first. A unit that was the FIRST this run started
    (it had the whole budget) and still did not finish never will: it is marked done and
    listed in ``skipped`` -- reported, never silently dropped. ``progress`` is called with
    ``{"done", "found", "skipped"}`` after every finished unit."""
    t0 = time.monotonic()
    lo, hi = boot - window_s, boot + 120
    found: List[Dict[str, str]] = []
    finished = list(done)
    skipped: List[str] = []
    complete = True
    started = 0

    def hit(kind: str, path: Path, action: str) -> None:
        found.append({"kind": kind, "path": str(path), "action": action})

    def left() -> float:
        return budget_s - (time.monotonic() - t0)

    def check() -> None:
        if left() <= 0:
            raise _OutOfBudgetError()

    def finish(key: str) -> None:
        finished.append(key)
        if progress is not None:
            progress({"done": list(finished), "found": list(found),
                      "skipped": list(skipped)})

    def cut_short(key: str) -> None:
        nonlocal complete
        complete = False
        if started == 1:
            skipped.append(key)
            finish(key)

    def ls_modified(tree: Path) -> subprocess.CompletedProcess:
        check()
        try:
            return _gitc(tree, "-c", "core.quotepath=off", "ls-files", "-m", "-z",
                         timeout=max(1, int(left()) + 1))
        except subprocess.TimeoutExpired:
            raise _OutOfBudgetError() from None

    for common in repos:
        for name in ("HEAD", "packed-refs"):
            if _all_nul(common / name):
                hit("git-meta", common / name, "reported (not guessed)")
        for tree, gd in _checkouts(Path(common)):
            key = _norm(tree)
            if key in finished:
                continue
            if left() <= 0:
                complete = False
                break
            started += 1
            try:
                _sweep_checkout(tree, gd, Path(common), fix=fix, lo=lo, hit=hit,
                                check=check, ls_modified=ls_modified)
            except _OutOfBudgetError:
                cut_short(key)
                break
            finish(key)
        if not complete:
            break
    home = aither_home if aither_home is not None else Path.home() / ".aither"
    # ~/.aither held 1.1M files on 2026-11-14 (a full walk: 140 s), so it is swept in
    # resumable units -- the loose files at its top, then one top-level directory each --
    # and the bulk stores that hold no small state are skipped.
    units: List[Path] = []
    if complete and home.is_dir():
        units = [home] + sorted(p for p in home.iterdir()
                                if p.is_dir() and p.name not in SCAN_SKIP_DIRS)
    for unit in units:
        key = "home:" + _norm(unit)
        if key in finished:
            continue
        if left() <= 0:
            complete = False
            break
        started += 1
        try:
            _sweep_home_unit(unit, unit == home, fix=fix, lo=lo, hi=hi, hit=hit,
                             check=check)
        except _OutOfBudgetError:
            cut_short(key)
            break
        finish(key)
    return {"boot": boot, "found": found, "done": finished, "complete": complete,
            "skipped": skipped, "seconds": round(time.monotonic() - t0, 1)}


def _sweep_checkout(tree: Path, gd: Path, common: Path, *, fix: bool, lo: float,
                    hit: Callable[[str, Path, str], None], check: Callable[[], None],
                    ls_modified: Callable[[Path], subprocess.CompletedProcess]) -> None:
    """One checkout of :func:`zero_scan`. Raises :class:`_OutOfBudgetError` only BETWEEN repairs:
    an index set aside is always rebuilt, a ``git checkout`` batch always runs to its end."""
    if _all_nul(gd / "HEAD"):
        hit("git-meta", gd / "HEAD", "reported (not guessed)")
    if gd != common and _all_nul(gd / "config.worktree"):
        hit("git-meta", gd / "config.worktree", "reported")
    idx = gd / "index"
    if _all_nul(idx):
        act = "needs --fix"
        if fix:
            stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            try:
                os.replace(idx, idx.with_name(f"index.zeroed-{stamp}"))
                r = _gitc(tree, "reset", "-q")
                act = "rebuilt from HEAD" if r.returncode == 0 else \
                    f"reset failed: {r.stderr.decode(errors='replace').strip()[:160]}"
            except OSError as exc:
                act = f"could not set aside: {exc}"
        hit("git-index", idx, act)
    stamps = []
    for p in (idx, gd / "HEAD", gd / "logs" / "HEAD"):
        try:
            stamps.append(p.stat().st_mtime)
        except OSError as exc:
            logger.debug("best-effort step skipped: %s", exc)
    if not stamps or max(stamps) < lo:  # not active in the window (or since)
        return
    r = ls_modified(tree)
    bad = []
    if r.returncode == 0:
        rels = list(filter(None, r.stdout.decode("utf-8", "replace").split("\0")))
        for n, rel in enumerate(rels, 1):
            if n % SCAN_CHECK_EVERY == 0:
                check()
            if _all_nul(tree / rel):
                bad.append(rel)
    for i in range(0, len(bad), 200):
        part = bad[i:i + 200]
        act = "needs --fix"
        if fix:
            rc = _gitc(tree, "checkout", "--", *part).returncode
            act = "restored from git" if rc == 0 else f"git checkout rc={rc}"
        for rel in part:
            hit("tracked", tree / rel, act)


def _sweep_home_unit(unit: Path, top: bool, *, fix: bool, lo: float, hi: float,
                     hit: Callable[[str, Path, str], None],
                     check: Callable[[], None]) -> None:
    """One ~/.aither unit of :func:`zero_scan`: the loose files of the top (``top``) or one
    whole top-level directory. Checks the budget per directory and every
    ``SCAN_CHECK_EVERY`` files (#16946: one directory held 371,942 files)."""
    if top:
        walker: Iterable[Tuple[str, List[str], List[str]]] = [
            (str(unit), [], [e.name for e in os.scandir(unit) if e.is_file()])]
    else:
        walker = os.walk(unit)
    n = 0
    for dirpath, dirnames, files in walker:
        check()
        dirnames[:] = [d for d in dirnames if d not in SCAN_SKIP_DIRS]
        for f in files:
            n += 1
            if n % SCAN_CHECK_EVERY == 0:
                check()
            if not f.endswith(SCAN_EXTS):
                continue
            p = Path(dirpath) / f
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if not (lo <= m <= hi) or not _all_nul(p):
                continue
            act = "reported (no .bak)"
            bak = p.with_name(p.name + ".bak")
            if bak.is_file() and not _all_nul(bak):
                act = "restorable from .bak (needs --fix)"
                if fix:
                    try:
                        write_atomic(p, bak.read_bytes())
                        act = "restored from .bak"
                    except OSError as exc:
                        act = f"restore failed: {exc}"
            hit("state", p, act)


def crash_scan_if_needed(repos: Sequence[Path], *, fix: bool,
                         budget_s: float = SCAN_BUDGET_S) -> Optional[Dict[str, object]]:
    """Run :func:`zero_scan` once per unclean boot (resuming until complete). None when
    there is nothing to do this tick. ``crash-scan.json`` is rewritten after finished
    units (at most every ``SCAN_SAVE_EVERY_S``), not only on return, so a run that is
    killed anyway keeps what it swept (#16946)."""
    boot = boot_time()
    sp = scan_state_path()
    try:
        st = json.loads(sp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    if abs(float(st.get("boot") or 0) - boot) < 120 and st.get("complete"):
        return None
    if abs(float(st.get("boot") or 0) - boot) >= 120:
        unclean, why = unclean_boot(boot)
        st = {"boot": boot, "unclean": unclean, "why": why, "done": [], "found": [],
              "complete": not unclean}
        if not unclean:
            write_atomic(sp, json.dumps(st).encode("utf-8"))
            return None
    base_found = list(st.get("found") or [])
    base_skipped = list(st.get("skipped") or [])
    last_save = [time.monotonic()]

    def merge(p: Dict, complete: bool) -> None:
        st.update(done=list(p["done"]), complete=complete,
                  found=_merge_found(base_found, p["found"]),
                  skipped=base_skipped + list(p["skipped"]))

    def progress(p: Dict) -> None:
        if time.monotonic() - last_save[0] < SCAN_SAVE_EVERY_S:
            return
        last_save[0] = time.monotonic()
        merge(p, False)
        try:
            write_atomic(sp, json.dumps(st).encode("utf-8"))
        except OSError as exc:  # the final write below still runs
            logger.debug("crash-scan progress not saved: %s", exc)

    res = zero_scan(repos, fix=fix, boot=boot, budget_s=budget_s, done=st.get("done") or [],
                    progress=progress)
    merge(res, bool(res["complete"]))
    write_atomic(sp, json.dumps(st).encode("utf-8"))
    res["found_total"] = st["found"]
    res["skipped_total"] = st["skipped"]
    res["why"] = st.get("why")
    return res


# ── CLI ───────────────────────────────────────────────────────────────────────

def add_parser(sub) -> None:
    p = sub.add_parser("integrity", help="check/snapshot/restore every managed repo's .git/config")
    p.add_argument("--fix", action="store_true",
                   help="restore a corrupt config from its last good copy")
    p.add_argument("--repo", action="append", default=[], help="a checkout or git dir (repeatable)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--keep", type=int, default=KEEP)
    p.add_argument("--prune-stale-branches", action="store_true",
                   help="list [branch] sections whose branch is gone (--apply removes them)")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--crash-scan", action="store_true",
                   help="sweep now for all-NUL files a crash left (git metadata, tracked files "
                        "of recently active checkouts, ~/.aither state); --fix restores")
    p.add_argument("--window-hours", type=float, default=SCAN_WINDOW_S / 3600)


def _resolve(raw: str) -> Optional[Path]:
    p = Path(raw)
    if (p / "HEAD").is_file() and not (p / ".git").exists():
        return p
    return common_dir_of(p)


def run(args) -> int:
    if args.repo:
        repos = [r for r in (_resolve(x) for x in args.repo) if r is not None]
        if not repos:
            print(f"awgit integrity: no git repo at {', '.join(args.repo)}", file=sys.stderr)
            return 2
    else:
        repos = discover()
    if args.crash_scan:
        res = zero_scan(repos, fix=args.fix, boot=time.time(), window_s=args.window_hours * 3600,
                        budget_s=3600)
        for f in res["found"]:
            print(json.dumps(f) if args.json else f"{f['kind']:<9} {f['action']:<28} {f['path']}")
        print(f"crash-scan: {len(res['found'])} all-NUL file(s); "
              f"{'complete' if res['complete'] else 'INCOMPLETE'} in {res['seconds']} s")
        return 1 if any("restored" not in f["action"] and "rebuilt" not in f["action"]
                        for f in res["found"]) else 0
    if args.prune_stale_branches:
        rows = [prune_stale_branches(r, apply=args.apply) for r in repos]
        for r in rows:
            print(json.dumps(r) if args.json else
                  f"{r['repo']}: {len(r['pruned'])} stale [branch] section(s)"
                  f"{' removed' if r.get('applied') else ''} {r.get('why', '')}".rstrip())
        return 0
    rows = sweep(repos, fix=args.fix, keep=args.keep)
    for r in rows:
        if args.json:
            print(json.dumps(r, sort_keys=True))
            continue
        line = f"{r['state'].upper():<11} {r['config']}"
        if r["state"] == OK and r.get("state_before"):
            line = f"RESTORED    {r['config']}  (was {r['state_before']}; from {r.get('from')})"
        elif r["action"] == "refused":
            line += f"  RESTORE REFUSED: {r.get('restore_why')}"
        elif r["action"] == "needs-fix":
            line += f"  ({r['why']}; run `awgit integrity --fix`)"
        elif r["action"] == "snapshot":
            line += f"  (snapshot taken; {r['snapshots']} kept)"
        print(line)
        for w in r.get("worktree_configs_corrupt") or []:
            print(f"  CORRUPT config.worktree {w}")
    if not rows:
        print("awgit integrity: no managed repo found (no checkout here, no configured roots)",
              file=sys.stderr)
    return exit_code(rows)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="awgit integrity")
    sub = ap.add_subparsers(dest="cmd")
    add_parser(sub)
    return run(ap.parse_args(["integrity", *(sys.argv[1:] if argv is None else argv)]))


if __name__ == "__main__":
    raise SystemExit(main())
