"""The evidence for awgit's own claim, computed from YOUR op-log.

awgit asserts something measurable: that agents working in one repo collide on
the same code, that git cannot see it, and that node identity plus leases can.
For a long time that was an argument. This turns it into a number anyone can
run against their own history:

    awgit evidence            # human summary
    awgit evidence --json     # machine-readable, for a dashboard or a badge

What it reports, and why each one:

  ops / actors        Is anything being captured, and by more than one identity?
                      A single-actor op-log CANNOT express a collision, so this
                      is the precondition for every other number being real.
  lease adoption      What share of captured work was leased. This was
                      unmeasurable until `leased` stopped being hardcoded.
  collisions          Nodes two or more actors have touched — the thing git
                      shows you nothing about.
  languages           Node identity is only as broad as the parser; a Python-only
                      install honestly reports Python-only coverage.

LOCAL AND OFFLINE, ALWAYS. This reads the op-log on disk and talks to nothing.
awgit is a version-control tool: it sees every line of proprietary code its user
writes, and a tool in that position that phones home — even "anonymously" — has
made a decision for its user that is not its to make. If aggregate sharing ever
happens it must be an explicit, separate, opt-IN action with the payload visible
first. `--json` exists so a user can look at exactly what they would share.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Optional

from awgit.oplog import OpLog


def _language_of(path: str) -> str:
    _, _, ext = (path or "").rpartition(".")
    return ext.lower() if ext else "(none)"


def gather(data_root=None, since: Optional[str] = None) -> dict:
    """Compute the evidence. Pure read of the op-log; no network, no writes."""
    log = OpLog(data_root=data_root)
    ops = log.ops_since(since) if since else log.all_ops()

    actors: Counter = Counter()
    leased = 0
    langs: Counter = Counter()
    touched: dict = defaultdict(set)      # (path, node_id) -> {actor}
    nodes = 0

    for op in ops:
        who = getattr(op, "actor", None) or "unknown"
        actors[who] += 1
        if getattr(op, "leased", False):
            leased += 1
        for ch in (getattr(op, "node_changes", None) or []):
            nid = getattr(ch, "node_id", None)
            if not nid:
                continue
            nodes += 1
            path = getattr(ch, "path", None) or "?"
            langs[_language_of(path)] += 1
            touched[(path, nid)].add(who)

    collisions = [
        {"path": p, "node_id": n, "actors": sorted(a)}
        for (p, n), a in touched.items() if len(a) > 1
    ]
    # Split the headline number, because the raw count OVER-STATES the claim.
    # An op-log that spans an attribution change contains collisions where the
    # same worker appears under an old label and a new one — real-looking, and
    # not evidence of two agents at all. Measured 2026-08-09 on this repo: 27
    # raw collisions, of which exactly ONE involved two distinct agent sessions.
    # Reporting 27 would have been a lie of aggregation.
    def _agent_sessions(actors: list) -> int:
        return len([a for a in actors if str(a).startswith("claude:")])

    confirmed = [c for c in collisions if _agent_sessions(c["actors"]) >= 2]
    ambiguous = [c for c in collisions if _agent_sessions(c["actors"]) < 2]
    total = len(ops)
    multi_actor = len(actors) > 1

    # Optional host enrichment: the op-log says what changed and who changed it,
    # never why. A host holding the agents' reasoning traces can answer that.
    # Absent is the normal state and yields no key at all — an explicit zero
    # would claim these agents worked without reasoning, which is a different
    # and false statement.
    from awgit import plugins as _plugins

    reasoning = _plugins.thoughts(sorted(actors))

    return {
        "ops": total,
        "node_changes": nodes,
        "distinct_nodes": len(touched),
        "actors": dict(actors),
        "actor_count": len(actors),
        "leased_ops": leased,
        "lease_adoption_pct": round(leased * 100 / total, 1) if total else 0.0,
        "languages": dict(langs.most_common(12)),
        "collisions": confirmed or ambiguous,
        "collision_count": len(collisions),
        # The number that actually supports the claim: two DISTINCT agent
        # sessions on one node. Everything else is a candidate, not evidence.
        "confirmed_multi_agent_collisions": len(confirmed),
        "ambiguous_collisions": len(ambiguous),
        # The honesty flag. Every collision number below is meaningless without
        # it, and a reader who does not know that will over-read a zero.
        "can_detect_collisions": multi_actor,
        # Present only when a host registered the THOUGHTS hook.
        **({"reasoning": reasoning} if reasoning else {}),
    }


def render(ev: dict) -> str:
    """Human summary. States what CANNOT be concluded as plainly as what can."""
    out = [
        "awgit evidence",
        f"  ops captured        {ev['ops']}",
        f"  node changes        {ev['node_changes']} across "
        f"{ev['distinct_nodes']} distinct nodes",
        f"  actors              {ev['actor_count']} "
        f"({', '.join(list(ev['actors'])[:3]) or 'none'})",
        f"  lease adoption      {ev['lease_adoption_pct']}% "
        f"({ev['leased_ops']}/{ev['ops']} ops leased)",
    ]
    if ev["languages"]:
        top = ", ".join(f".{k}:{v}" for k, v in list(ev["languages"].items())[:6])
        out.append(f"  languages           {top}")
    out.append(f"  node collisions     {ev['collision_count']} raw — "
               f"{ev.get('confirmed_multi_agent_collisions', 0)} CONFIRMED "
               f"(two distinct agent sessions), "
               f"{ev.get('ambiguous_collisions', 0)} ambiguous")
    reasoning = ev.get("reasoning")
    if reasoning:
        traces = reasoning.get("traces")
        linked = reasoning.get("linked_actors")
        detail = f"{traces} trace(s)" if traces is not None else "available"
        if linked is not None:
            detail += f" across {linked} actor(s)"
        out.append(f"  reasoning captured  {detail}")
    if not ev["can_detect_collisions"]:
        out.append("")
        out.append("  NOTE: only ONE actor appears in this op-log, so a collision")
        out.append("  is not merely absent — it is INEXPRESSIBLE. Read the count")
        out.append("  as 'not measured', never as 'none happened'.")
    for c in ev["collisions"][:5]:
        out.append(f"    {c['path']}  {c['node_id'][:16]}  {', '.join(c['actors'])}")
    return "\n".join(out)


def to_json(ev: dict) -> str:
    return json.dumps(ev, indent=2, sort_keys=True)
