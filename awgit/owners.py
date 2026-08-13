"""Who owns this code — measured, not declared.

A CODEOWNERS file says who *should* review a path. It is written once, by
whoever set the repo up, and then it rots: people leave, subsystems change
hands, and the file keeps confidently naming them. Nothing ever contradicts it,
because nothing measures the alternative.

awgit already has the alternative. The op-log records who changed which NODE,
under a verified GitHub identity, on every commit. So "who owns this" can be
answered from what people actually did:

    ownership = share of RECENT changes to the nodes in this path

Both answers are shown, deliberately. The declared owner is who is accountable;
the measured owner is who has been in the code. When they disagree that is the
interesting part — usually the file is stale, occasionally the reviewer is — and
hiding either would turn a useful disagreement into a silent wrong answer.

Recency is weighted because ownership decays: someone who wrote a module two
years ago and has not touched it since is not who you want reviewing it today.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: Changes older than this contribute nothing. Ownership is about who is in the
#: code NOW; an unbounded window makes the original author permanent owner of
#: everything they ever started.
HALF_LIFE_DAYS = 90.0


@dataclass
class Owner:
    actor: str
    verified: bool
    score: float
    changes: int

    def to_dict(self) -> dict:
        return {"actor": self.actor, "verified": self.verified,
                "score": round(self.score, 3), "changes": self.changes}


def _age_days(ts: str) -> float:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = time.strptime(ts.replace("Z", "+0000"), fmt)
            return max(0.0, (time.time() - time.mktime(parsed)) / 86400.0)
        except (ValueError, OverflowError):
            continue
    return HALF_LIFE_DAYS  # unparseable timestamp: one half-life, not zero, not infinite


def _weight(ts: str) -> float:
    return 0.5 ** (_age_days(ts) / HALF_LIFE_DAYS)


def measured(path_prefix: str = "", data_root: Optional[Path] = None,
             limit: int = 5) -> List[Owner]:
    """Owners by recency-weighted share of node changes under ``path_prefix``."""
    from awgit.oplog import OpLog

    log = OpLog(data_root=data_root) if data_root else OpLog()
    try:
        ops = log.all_ops()
    except Exception:  # noqa: BLE001 - no op-log is "unknown", handled by the caller
        return []

    scores: Dict[str, float] = {}
    counts: Dict[str, int] = {}
    verified: Dict[str, bool] = {}
    prefix = path_prefix.replace("\\", "/").strip("/")
    for op in ops:
        touched = [nc for nc in op.node_changes
                   if not prefix
                   or (nc.path or "").replace("\\", "/").startswith(prefix)]
        if not touched:
            continue
        # The VERIFIED identity when there is one: a self-asserted actor is
        # forgeable, and ownership is exactly the question where that matters.
        who = op.verified_actor if op.actor_verified and op.verified_actor else op.actor
        scores[who] = scores.get(who, 0.0) + _weight(op.ts) * len(touched)
        counts[who] = counts.get(who, 0) + len(touched)
        verified[who] = verified.get(who, False) or bool(op.actor_verified)

    total = sum(scores.values()) or 1.0
    ranked = [Owner(actor=a, verified=verified.get(a, False),
                    score=s / total, changes=counts[a])
              for a, s in scores.items()]
    ranked.sort(key=lambda o: (-o.score, o.actor))
    return ranked[:limit]


def declared(path: str, repo: Optional[Path] = None) -> List[str]:
    """Owners from CODEOWNERS, if the repo has one. Last match wins, as git does."""
    base = Path(repo or ".")
    for candidate in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        found = base / candidate
        if found.is_file():
            return _match(found.read_text(encoding="utf-8", errors="replace"), path)
    return []


def _match(text: str, path: str) -> List[str]:
    """CODEOWNERS matching: LAST matching rule wins, which is git's rule.

    Getting the precedence backwards is not a crash, it is a confidently wrong
    reviewer — the generic `*` rule at the top of most files would beat every
    specific rule below it.
    """
    target = path.replace("\\", "/").lstrip("/")
    winners: List[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        pattern, holders = parts[0], parts[1:]
        if not holders:
            continue
        if _glob(pattern, target):
            winners = holders
    return winners


def _glob(pattern: str, target: str) -> bool:
    """CODEOWNERS pattern matching, with gitignore's depth rule.

    A pattern containing NO slash matches a file's basename at ANY depth —
    `*.py` owns `AitherOS/lib/x.py`, not just `x.py` at the root. Anchoring
    everything to the root is the natural way to write this and it is wrong for
    the commonest pattern in every real CODEOWNERS file, so the rule would
    silently return "no declared owner" for most of the repo.
    """
    anchored = "/" in pattern.rstrip("/")
    pattern = pattern.lstrip("/")
    if pattern in ("*", "**"):
        return True
    if pattern.endswith("/"):
        prefix = pattern if anchored else ""
        return target.startswith(prefix) if prefix else f"/{pattern}" in f"/{target}"
    regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    if anchored:
        return bool(re.match(f"^{regex}(/.*)?$", target))
    # Unanchored: match the basename, or any full path segment run.
    return bool(re.match(f"^{regex}$", target.rsplit("/", 1)[-1])
                or re.match(f"^(.*/)?{regex}(/.*)?$", target))


def report(path: str, repo: Optional[Path] = None,
           data_root: Optional[Path] = None) -> Tuple[List[str], List[Owner]]:
    return declared(path, repo), measured(path, data_root)


def render(path: str, decl: List[str], meas: List[Owner]) -> List[str]:
    lines = [f"  {path or '(whole repo)'}"]
    lines.append(f"    declared: {', '.join(decl) if decl else '(no CODEOWNERS rule)'}")
    if not meas:
        lines.append("    measured: (no op-log history for this path)")
        return lines
    lines.append("    measured:")
    for owner in meas:
        mark = "verified" if owner.verified else "asserted"
        lines.append(f"      {owner.score:5.1%}  {owner.actor}  "
                     f"({owner.changes} node changes, {mark})")
    return lines
