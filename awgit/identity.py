"""Verified-identity → attribution resolution (standalone).

The verified actor is the GitHub identity (the Aitherium GitHub OAuth-app login
via ``gh``). This module makes it the AUTHORITATIVE attribution identity:

  - ``attribution_id(login)`` → ``github:<login>`` — a deterministic namespace
    so the GitHub identity can never collide with a platform ``user_id``;
  - ``github_email()`` → the gh account's primary email (cached, best-effort).

Standalone awgit records attribution only — who changed what, under a verified
GitHub identity, with a deterministic handle per op. A downstream AitherOS
reward program that consumes this attribution lives in the AitherOS monorepo's
internal awgit, not in the public package.
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


def attribution_id(verified_actor: str) -> str:
    """The authoritative attribution identity for a verified GitHub login."""
    return f"github:{verified_actor}"


def github_email(data_root: Optional[Path] = None) -> str:
    """Primary email of the gh-authenticated account (cached, best-effort).

    Returns "" on any failure (no ``gh``, offline, unauthenticated) — identity
    resolution never blocks attribution.
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
