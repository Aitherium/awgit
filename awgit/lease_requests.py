"""Ask the holder of a lease to release it — and let them find out.

WHY THIS EXISTS

The lease plane tells a blocked session exactly one thing: ``lease conflict:
'<path>' held by 'claude:c0a84ba5-...' until 21:37``. The concurrent-safe-git
skill then says "talk to them or wait for the lease to expire, do NOT force past
it" -- and there has never been a way to talk to them. So in practice every
conflict resolves as "wait", or as someone bypassing the gate.

Measured 2026-08-19: one session held **1,952 active leases** (a
``lease acquire --staged --adopt`` over a 4,073-file dirty index) while three
other sessions held 3, 3 and 1. Every one of those sessions was refused on files
the holder had never opened, for the two hours until the pile expired. Neither
side could see the problem: each lease was individually valid, the holder saw a
successful bulk acquire, and the blocked sessions saw a conflict naming an actor
they had no way to reach.

WHY A SEPARATE FILE

Requests live in ``lease_requests.json``, NOT inside ``leases.json``. The lease
store is the thing every session on the box depends on to commit at all, it is
mutated under a lock by many processes, and a serialization change there is a
fleet-wide outage if it goes wrong. A sidecar file cannot corrupt it, can be
read independently, and is safe to delete.

WHY IT REACHES THE HOLDER

An agent session may never read a chat channel, but it WILL run ``awgit`` again
-- that is the one thing it is guaranteed to do, because it cannot commit
without it. So the durable delivery path is the tool itself: pending requests
surface in ``awgit lease list`` and ``awgit lease requests``. Relay is the
best-effort async path on top, never the only one; a notification that depends
on someone watching a channel is not delivery.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: A request older than this is dropped on read. A lease's own TTL is far
#: shorter, so a request outliving it is about a lease that no longer exists.
REQUEST_TTL_SEC = 6 * 3600

#: Never let the sidecar grow without bound; a runaway loop must not fill the
#: disk that Postgres shares (that has taken this box down before).
MAX_REQUESTS = 500


def _now() -> float:
    return time.time()


class LeaseRequests:
    """Durable, append-mostly store of 'please release this' asks.

    Deliberately tolerant: a corrupt or unreadable sidecar degrades to EMPTY and
    logs, exactly as the lease registry does. This must never be able to stop a
    commit -- it exists to unblock people, and a request store that can block
    them is worse than no request store.
    """

    def __init__(self, data_root: Path) -> None:
        self._path = Path(data_root) / "lease_requests.json"

    # ── persistence ──────────────────────────────────────────────────────

    def _load(self) -> List[Dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            items = payload.get("requests", [])
            if not isinstance(items, list):
                return []
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("[vcs.requests] unreadable (%s); treating as empty", exc)
            return []
        cutoff = _now() - REQUEST_TTL_SEC
        return [r for r in items
                if isinstance(r, dict) and float(r.get("ts", 0) or 0) >= cutoff]

    def _save(self, items: List[Dict[str, Any]]) -> None:
        items = items[-MAX_REQUESTS:]
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"requests": items}, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self._path)      # atomic; a torn file is unreadable
        except OSError as exc:
            # Loud, never silent -- but never fatal either.
            logger.warning("[vcs.requests] could not save (%s)", exc)

    # ── API ──────────────────────────────────────────────────────────────

    def add(self, target: str, to_actor: str, from_actor: str,
            message: str = "") -> Dict[str, Any]:
        """Record 'from_actor asks to_actor to release target'.

        Idempotent per (target, from, to): re-asking updates the existing entry
        rather than stacking duplicates, so a retry loop cannot spam the holder
        into ignoring the whole mechanism.
        """
        items = self._load()
        for r in items:
            if (r.get("target") == target and r.get("to_actor") == to_actor
                    and r.get("from_actor") == from_actor):
                r["message"] = message or r.get("message", "")
                r["ts"] = _now()
                r["count"] = int(r.get("count", 1)) + 1
                self._save(items)
                return r
        rec = {"target": target, "to_actor": to_actor, "from_actor": from_actor,
               "message": message, "ts": _now(), "count": 1}
        items.append(rec)
        self._save(items)
        return rec

    def for_actor(self, actor: str) -> List[Dict[str, Any]]:
        """Requests addressed TO this actor -- what they are blocking."""
        return [r for r in self._load() if r.get("to_actor") == actor]

    def by_actor(self, actor: str) -> List[Dict[str, Any]]:
        """Requests this actor has SENT -- what they are waiting on."""
        return [r for r in self._load() if r.get("from_actor") == actor]

    def clear(self, actor: str, targets: Optional[List[str]] = None) -> int:
        """Drop requests addressed to `actor` (all, or just `targets`)."""
        items = self._load()
        keep, dropped = [], 0
        for r in items:
            hit = r.get("to_actor") == actor and (
                targets is None or r.get("target") in targets)
            if hit:
                dropped += 1
            else:
                keep.append(r)
        if dropped:
            self._save(keep)
        return dropped


def format_pending(requests: List[Dict[str, Any]], limit: int = 10) -> str:
    """One block a human or an agent can act on directly.

    Says WHAT to run, because a notification that leaves the reader to work out
    the command is how this ends up ignored.
    """
    if not requests:
        return ""
    lines = [f"vcs: {len(requests)} session(s) are BLOCKED on leases you hold:"]
    for r in requests[:limit]:
        who = str(r.get("from_actor", "?")).split(":")[-1][:8]
        msg = (r.get("message") or "").strip()
        again = f" (asked {r['count']}x)" if int(r.get("count", 1)) > 1 else ""
        lines.append(f"vcs:   {r.get('target')}  <- {who}{again}"
                     + (f"  {msg!r}" if msg else ""))
    if len(requests) > limit:
        lines.append(f"vcs:   ... and {len(requests) - limit} more")
    lines.append("vcs: release what you are not editing:")
    lines.append("vcs:   awgit lease list            # your lease ids")
    lines.append("vcs:   awgit lease release <ids>   # frees them immediately")
    return "\n".join(lines)
