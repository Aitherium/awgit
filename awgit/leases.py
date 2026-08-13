"""Lease registry — conflict PREVENTION for concurrent agents.

An agent registers intent ("I am editing nodes/files X") before editing; the
registry grants or rejects so two agents never collide. Leases are
heartbeat-renewed TTLs (the ``xPageDriverAt`` pattern): a lease self-heals when
its holder disappears — expiry frees the target, never a kill switch.

All-or-nothing acquire: if ANY requested target is held by another actor's
ACTIVE lease, the whole batch is rejected naming the conflicting holder.

The store is ``Library/Data/vcs/leases.json`` (gitignored). Mutations run under
the same cross-process ``FileLock`` as the op-log.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from awgit.data_root import vcs_data_root
from awgit.oplog import FileLock

logger = logging.getLogger(__name__)

DEFAULT_TTL_SEC = 300


def _iso(offset_sec: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_sec)).isoformat()


@dataclass
class Lease:
    lease_id: str
    actor: str
    kind: str  # "node" | "path"
    target: str
    granted_ts: str
    expires_ts: str
    heartbeat_ts: str
    ttl_sec: int = DEFAULT_TTL_SEC
    reason: str = ""
    status: str = "active"  # active | expired | revoked | released
    #: Content of the file at the moment the lease was granted, as a git blob sha.
    #: This is what makes "which edits are MINE" computable rather than guessable:
    #: my edits are exactly (working tree - baseline), and everything else in the
    #: file belongs to whoever else is writing it. Empty for node leases and for
    #: paths that did not exist or could not be hashed.
    baseline_blob: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "actor": self.actor,
            "kind": self.kind,
            "target": self.target,
            "granted_ts": self.granted_ts,
            "expires_ts": self.expires_ts,
            "heartbeat_ts": self.heartbeat_ts,
            "ttl_sec": self.ttl_sec,
            "reason": self.reason,
            "status": self.status,
            "baseline_blob": self.baseline_blob,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, object]) -> "Lease":
        return cls(
            lease_id=str(d["lease_id"]),
            actor=str(d["actor"]),
            kind=str(d.get("kind", "path")),
            target=str(d["target"]),
            granted_ts=str(d.get("granted_ts", "")),
            expires_ts=str(d.get("expires_ts", "")),
            heartbeat_ts=str(d.get("heartbeat_ts", "")),
            ttl_sec=int(d.get("ttl_sec", DEFAULT_TTL_SEC)),
            reason=str(d.get("reason", "")),
            status=str(d.get("status", "active")),
            baseline_blob=str(d.get("baseline_blob", "")),
        )


def snapshot_baseline(rel_path: str, repo: Optional[Path] = None) -> str:
    """Store the file's current bytes as a git blob and return its sha.

    Uses ``git hash-object -w`` so the snapshot lives in the object database the
    repo already has — no parallel content store to grow, prune or corrupt, and
    the blob is readable later with ``git cat-file``.

    Returns "" when there is nothing to snapshot (the path does not exist yet, or
    git is unavailable). An empty baseline is handled explicitly downstream rather
    than being treated as an empty FILE, because those mean opposite things: "I
    have no record" must not silently become "the file was empty, so everything in
    it is yours".
    """
    root = repo or Path.cwd()
    target = root / rel_path
    if not target.is_file():
        return ""
    try:
        proc = subprocess.run(
            ["git", "hash-object", "-w", "--", rel_path],
            cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("[vcs.leases] baseline snapshot failed for %s: %s", rel_path, exc)
        return ""
    if proc.returncode != 0:
        logger.warning(
            "[vcs.leases] baseline snapshot failed for %s: %s",
            rel_path, (proc.stderr or "").strip(),
        )
        return ""
    return proc.stdout.strip()


class LeaseConflictError(Exception):
    """All-or-nothing acquire collided with another actor's active lease."""

    def __init__(self, holder: "Lease"):
        self.holder = holder
        super().__init__(
            f"lease conflict: {holder.target!r} held by {holder.actor!r} "
            f"until {holder.expires_ts}"
        )


class LeaseRegistry:
    """Heartbeat-renewed TTL lease registry (durable, cross-process)."""

    def __init__(self, data_root: Optional[Path] = None) -> None:
        self._data_root = data_root or vcs_data_root()
        self._path = self._data_root / "leases.json"
        self._leases: Dict[str, Lease] = {}
        self._reload()

    # ── persistence (always under the store lock) ────────────────────────

    def _reload(self) -> None:
        self._leases = {}
        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            for d in payload.get("leases", []):
                lz = Lease.from_dict(d)
                self._leases[lz.lease_id] = lz
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            # Loud degrade, never silent: a corrupt store is empty + logged.
            logger.warning("[vcs.leases] reload failed (%s); treating as empty", exc)

    def _save(self) -> None:
        self._data_root.mkdir(parents=True, exist_ok=True)
        payload = {"leases": [lz.to_dict() for lz in self._leases.values()]}
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    # ── core ops ─────────────────────────────────────────────────────────

    def acquire(
        self,
        actor: str,
        targets: List[str],
        *,
        ttl_sec: int = DEFAULT_TTL_SEC,
        reason: str = "",
        kind: Optional[str] = None,
    ) -> List[Lease]:
        """Grant a batch, or raise ``LeaseConflict`` naming the blocker.

        All-or-nothing: if ANY target is held by another actor's active lease,
        NO target is granted. Re-acquiring targets the SAME actor already holds
        extends them (idempotent). Expired leases are swept first.
        """
        with FileLock(self._data_root / "leases.lock"):
            self._reload()
            self._mark_expired()
            now = _iso()
            active_by_target: Dict[str, List[Lease]] = {}
            for lz in self._leases.values():
                if lz.status == "active":
                    active_by_target.setdefault(lz.target, []).append(lz)
            for t in targets:
                for lz in active_by_target.get(t, []):
                    if lz.actor != actor:
                        raise LeaseConflictError(lz)
            granted: List[Lease] = []
            for t in targets:
                existing = [
                    lz for lz in active_by_target.get(t, []) if lz.actor == actor
                ]
                if existing:
                    lz = existing[0]
                    lz.expires_ts = _iso(ttl_sec)
                    lz.heartbeat_ts = now
                    lz.reason = reason
                    granted.append(lz)
                else:
                    k = kind or ("node" if t.startswith("node_") else "path")
                    lz = Lease(
                        lease_id=uuid.uuid4().hex,
                        actor=actor,
                        kind=k,
                        target=t,
                        granted_ts=now,
                        expires_ts=_iso(ttl_sec),
                        heartbeat_ts=now,
                        ttl_sec=ttl_sec,
                        reason=reason,
                        # Snapshot NOW, before any edit — this is the whole point
                        # of taking the lease before you start typing.
                        baseline_blob=(snapshot_baseline(t) if k == "path" else ""),
                    )
                    self._leases[lz.lease_id] = lz
                    granted.append(lz)
            self._save()
            return granted

    def heartbeat(self, actor: str, lease_ids: List[str]) -> int:
        """Refresh TTL for the actor's active leases; returns count refreshed."""
        with FileLock(self._data_root / "leases.lock"):
            self._reload()
            now = _iso()
            count = 0
            for lid in lease_ids:
                lz = self._leases.get(lid)
                if lz and lz.actor == actor and lz.status == "active":
                    lz.expires_ts = _iso(lz.ttl_sec)
                    lz.heartbeat_ts = now
                    count += 1
            if count:
                self._save()
            return count

    def release(self, actor: str, lease_ids: List[str]) -> int:
        """Mark the actor's leases released (frees targets immediately)."""
        with FileLock(self._data_root / "leases.lock"):
            self._reload()
            now = _iso()
            count = 0
            for lid in lease_ids:
                lz = self._leases.get(lid)
                if lz and lz.actor == actor and lz.status == "active":
                    lz.status = "released"
                    lz.heartbeat_ts = now
                    count += 1
            if count:
                self._save()
            return count

    def sweep_expired(self) -> int:
        """Mark expired leases 'expired'; returns count swept."""
        with FileLock(self._data_root / "leases.lock"):
            self._reload()
            n = self._mark_expired()
            if n:
                self._save()
            return n

    def _mark_expired(self) -> int:
        now = _iso()
        n = 0
        for lz in self._leases.values():
            # <= not <: a lease whose expiry has been REACHED is expired.
            # With strict <, a ttl=0 lease granted and checked in the same
            # microsecond never expires (a real timing race the tests caught).
            if lz.status == "active" and lz.expires_ts <= now:
                lz.status = "expired"
                n += 1
        return n

    # ── queries ──────────────────────────────────────────────────────────

    def get(self, lease_id: str) -> Optional[Lease]:
        return self._leases.get(lease_id)

    def active_leases(self) -> List[Lease]:
        return [lz for lz in self._leases.values() if lz.status == "active"]

    def leases_by_actor(self, actor: str) -> List[Lease]:
        return [lz for lz in self._leases.values() if lz.actor == actor]


# Files worth guarding: the ones two agents plausibly edit at the same time.
# Widened from ".py only" on 2026-08-09. The old scope let a `.yml`
# commit print "lease-check OK" while checking NOTHING, so compose files,
# workflows, skills and .mcp.json were exactly as clobberable as before the
# gate existed — and that same day edits to docker-compose.aitheros.yml and
# concurrent-safe-git/SKILL.md had to be hand-restored after a concurrent
# overwrite, while the .py the gate did cover was overwritten four times.
GUARDED_SUFFIXES = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs",          # source
    ".yml", ".yaml", ".toml", ".json", ".ini", ".cfg",    # config
    ".sh", ".bash", ".ps1", ".psm1",                      # scripts
    ".md",                                                # rules, skills, docs
    ".sql", ".proto", ".env",
)
# Bulk content nobody races on. Guarding these would make a routine content
# commit need dozens of leases, and a gate that heavy gets ROUTED AROUND rather
# than satisfied — the exact failure that produced this repo's per-file-ignores.
UNGUARDED_PREFIXES = (
    "AitherOS/apps/AitherVeil/content/",   # blog posts, one author at a time
    "AitherOS/Library/",                   # generated training/runtime data
    ".DELIVERABLES/",                      # published artifacts
    ".RESEARCH/INTAKE/",                   # dossiers
    "TECH_DEBT_ARCHIVE",                   # frozen
)


def is_guarded(path: str) -> bool:
    """Does the lease gate care about this path?

    Extension-based with a bulk-content escape, rather than "everything": the
    trade is deliberate. Covering every path means a 50-file commit needs 50
    leases; covering none is where this started.
    """
    p = (path or "").replace("\\", "/").strip()
    if not p:
        return False
    if p.startswith(UNGUARDED_PREFIXES):
        return False
    return p.endswith(GUARDED_SUFFIXES)


def coverage_gap(
    changed_files: List[str],
    actor: str,
    data_root: Optional[Path] = None,
) -> List[str]:
    """Changed GUARDED files not covered by the actor's active path-leases.

    The pre-commit gate's primitive: a commit touching a guarded file with no
    active path-lease is an unleased edit — rejected when leases are enforced
    (VCS_LEASES_ENFORCE=1), recorded as ``leased=false`` otherwise.

    ``awgit lease acquire --staged`` takes leases on exactly this set, so
    complying is one command rather than a per-file chore.
    """
    registry = LeaseRegistry(data_root=data_root)
    registry.sweep_expired()
    covered = {
        lz.target
        for lz in registry.active_leases()
        if lz.actor == actor and lz.kind == "path"
    }
    return [f for f in changed_files if is_guarded(f) and f not in covered]
