"""ACTA earning — submit op-ledger contributions for credit (Phase 6, slice 2).

Grounded on AitherACTA's real contract (``services/orchestration/AitherACTA.py``):
``POST /v1/billing/credit`` takes ``{user_id, tokens, reason, source}`` +
``X-Internal-Token`` (AITHER_INTERNAL_SECRET) and credits a user's balance,
recording an accounting-ledger event. It is used by the ARC playground to
reward validated contributions — the SAME earning model the op-log is the twin
of.

The endpoint is NOT idempotent, so the CALLER owns idempotency: a submitted
ledger (``submitted.json`` in the vcs data root) records which ``ledger_ref`` s
have been credited, and a re-submit is a safe no-op — **each op earns at most
once, ever** (``ledger_ref`` is the deterministic handle from ``ledger.py``).

EARNING, not GATING: nothing here affects whether a commit lands. Crediting is
an explicit, dry-run-first action — ``awgit ledger --credit <sha> [--apply]``.
The ``user_id`` is the ACTA platform user; mapping ``verified_actor`` (a GitHub
login) to it is the AitherIdentity/Directory cross-check (next roadmap item) —
until then the caller passes it explicitly.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from awgit.data_root import vcs_data_root
from awgit.identity import acta_user_id
from awgit.ledger import LedgerEntry
from awgit.oplog import FileLock


def contribution_record(
    entry: LedgerEntry,
    tokens: int,
    reason: str = "",
    source: str = "awgit",
    user_id: str = "",
) -> Dict[str, object]:
    """The ``BillingCreditRequest`` shape for ``POST /v1/billing/credit``.

    ``user_id`` overrides the ACTA user when given; otherwise a VERIFIED
    identity maps to the authoritative ``github:<login>`` (``identity.py`` —
    never collides with a platform user_id), else the claimed actor.
    """
    return {
        "user_id": user_id
        or (
            acta_user_id(entry.verified_actor)
            if entry.actor_verified and entry.verified_actor
            else entry.actor
        ),
        "tokens": tokens,
        "reason": reason or f"vcs-contribution {entry.ledger_ref}",
        "source": source,
    }


def _submitted_path(data_root: Optional[Path]) -> Path:
    return (data_root or vcs_data_root()) / "submitted.json"


def was_submitted(ledger_ref: str, data_root: Optional[Path] = None) -> bool:
    """True if this op's contribution was already credited (idempotency)."""
    path = _submitted_path(data_root)
    if not path.exists():
        return False
    try:
        return ledger_ref in json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False


def mark_submitted(
    ledger_ref: str, record: Dict[str, object], data_root: Optional[Path] = None
) -> None:
    """Record that a contribution was submitted (durable, atomic)."""
    path = _submitted_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(path.with_suffix(".lock")):
        recs: Dict[str, object] = {}
        if path.exists():
            try:
                recs = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                recs = {}
        recs[ledger_ref] = {
            **record,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(recs), encoding="utf-8")
        os.replace(tmp, path)


def submit_contribution(
    entry: LedgerEntry,
    tokens: int,
    *,
    base_url: str = "",
    reason: str = "",
    user_id: str = "",
    data_root: Optional[Path] = None,
    apply: bool = False,
) -> Dict[str, object]:
    """Submit an op's contribution for ACTA credit (dry-run by default).

    - already submitted → ``{ok: False, skipped: "already submitted"}`` — each
      op earns at most once, ever;
    - ``apply=False`` → builds the request, returns it as a dry-run (no POST);
    - ``apply=True`` → POSTs ``/v1/billing/credit`` with ``X-Internal-Token``
      (read fresh), marks the ledger_ref submitted ONLY on success (a failure
      leaves it unsubmitted so it can be retried — never a silent double-credit,
      never a silent fake-success).
    """
    record = contribution_record(entry, tokens, reason, user_id=user_id)
    if was_submitted(entry.ledger_ref, data_root):
        return {
            "ok": False,
            "skipped": "already submitted",
            "ledger_ref": entry.ledger_ref,
        }
    if not apply:
        return {
            "ok": None,
            "dry_run": True,
            "request": record,
            "ledger_ref": entry.ledger_ref,
        }
    base = base_url or os.environ.get("ACTA_SERVICE_URL", "http://localhost:8001")
    key = os.environ.get("AITHER_INTERNAL_SECRET", "")
    headers = {"X-Internal-Token": key, "Content-Type": "application/json"}
    try:
        import httpx

        # internal CA trust, never verify=False (SEC001)
        resp = httpx.post(
            f"{base}/v1/billing/credit",
            json=record, headers=headers, timeout=10,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - surfaced in the result
        return {"ok": False, "error": str(exc), "ledger_ref": entry.ledger_ref}
    mark_submitted(entry.ledger_ref, record, data_root)
    return {
        "ok": True,
        "ledger_ref": entry.ledger_ref,
        "response": resp.json(),
    }
