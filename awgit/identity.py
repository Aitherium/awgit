"""Verified-identity → ACTA user resolution (Phase 6, slice 3).

The verified actor is the GitHub identity (the Aitherium GitHub OAuth-app login
via ``gh``). This module makes it the AUTHORITATIVE ACTA user:

  - ``acta_user_id(login)`` → ``github:<login>`` — a deterministic namespace so
    the GitHub identity can never collide with a platform ``user_id``;
  - ``github_email()`` → the gh account's primary email (for the user record,
    cached);
  - ``provision_acta_user(...)`` → POST ``/v1/internal/ensure-user`` so the
    billing record exists before a credit (idempotent — returns ``exists``).

The GitHub→platform cross-check (routing a credit to an EXISTING platform
balance by email match) is the AitherIdentity/Directory slice; until it lands,
``--user <platform-id>`` overrides to route to a specific platform balance.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

from awgit.data_root import vcs_data_root

logger = logging.getLogger(__name__)

_TTL_SEC = 6 * 3600  # refresh the GitHub identity at most this often


def acta_user_id(verified_actor: str) -> str:
    """The authoritative ACTA user for a verified GitHub identity."""
    return f"github:{verified_actor}"


def github_email(data_root: Optional[Path] = None) -> str:
    """Primary email of the gh-authenticated account (cached, best-effort).

    Returns "" on any failure (no ``gh``, offline, unauthenticated) — identity
    resolution never blocks crediting.
    """
    data = data_root or vcs_data_root()
    cache = data / "identity.json"
    if cache.exists():
        try:
            rec = json.loads(cache.read_text(encoding="utf-8"))
            if rec.get("email") and time.time() - rec.get("ts", 0) < _TTL_SEC:
                return rec["email"]
        except (OSError, ValueError):
            logger.debug("vcs: identity email cache unreadable, re-resolving")
    try:
        out = subprocess.run(
            ["gh", "api", "user/emails", "--jq", ".[] | select(.primary) | .email"],
            capture_output=True, text=True, encoding="utf-8",
            timeout=5, check=True,
        ).stdout.strip().splitlines()
        email = out[0] if out else ""
    except (OSError, subprocess.SubprocessError):
        return ""
    if email:
        try:
            data.mkdir(parents=True, exist_ok=True)
            rec: Dict[str, object] = {"ts": time.time(), "email": email}
            try:
                existing = (
                    json.loads(cache.read_text(encoding="utf-8"))
                    if cache.exists() else {}
                )
                if existing.get("login"):
                    rec["login"] = existing["login"]
            except (OSError, ValueError):
                logger.debug("vcs: identity cache unreadable while merging")
            tmp = cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(rec), encoding="utf-8")
            os.replace(tmp, cache)
        except OSError:
            logger.debug("vcs: identity email cache write failed (best-effort)")
    return email


def provision_acta_user(
    user_id: str,
    *,
    email: str = "",
    base_url: str = "",
    apply: bool = False,
) -> Dict[str, object]:
    """POST ``/v1/internal/ensure-user`` — ensure the ACTA billing record.

    Dry-run by default (builds the request); ``apply=True`` posts with
    ``X-Internal-Token`` (never ``verify=False``). Idempotent: an existing
    record returns ``exists``.
    """
    base = base_url or os.environ.get("ACTA_SERVICE_URL", "http://localhost:8001")
    key = os.environ.get("AITHER_INTERNAL_SECRET", "")
    body = {"user_id": user_id, "email": email}
    headers = {"X-Internal-Token": key, "Content-Type": "application/json"}
    if not apply:
        return {"ok": None, "dry_run": True, "request": body}
    try:
        import httpx

        resp = httpx.post(
            f"{base}/v1/internal/ensure-user",
            json=body, headers=headers, timeout=10,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - surfaced in the result
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "response": resp.json()}
