"""Edit-op capture — turn a git commit into a semantic ``EditOp``.

Flow: changed files (git) → for each Python file parse the parent and commit
blobs via ``parse_source_bytes`` → diff the node sets keyed by stable node id
(``StableNodeIDManager``) → emit ``NodeChange`` records whose body hashes come
from the EXACT commit blobs, never the live index. That is the anti-staleness
guarantee: node *identity* is graph-derived, body *content* is commit-derived,
so an op stays self-consistent even if the index has moved on (D-1640 class).

Python-tree-first: non-``.py`` files are recorded in ``file_paths`` but skipped
at the node level in M1 (file-level fallback ops are a later refinement).
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from awgit.bodies import BodyStore
from awgit.bodies import blob_sha as _blob_sha
from awgit.data_root import vcs_data_root
from awgit.ledger import mint_ledger_ref
from awgit.oplog import FileLock, OpLog
from awgit.schema import EditOp, NodeChange

logger = logging.getLogger(__name__)


def default_repo() -> Path:
    return Path(os.environ.get("VCS_REPO_ROOT", os.getcwd()))


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, check=True
    ).stdout


def git_parent(repo: Path, sha: str) -> Optional[str]:
    out = _git(repo, "rev-list", "--parents", "-n", "1", sha).decode().split()
    return out[1] if len(out) > 1 else None


def git_changed_files(repo: Path, sha: str) -> List[str]:
    out = _git(repo, "show", "--format=", "--name-only", sha).decode()
    return [ln for ln in out.splitlines() if ln.strip()]


def git_blob(repo: Path, sha: str, rel_path: str) -> Optional[bytes]:
    try:
        return _git(repo, "show", f"{sha}:{rel_path}")
    except subprocess.CalledProcessError:
        return None


# ── actor provenance ──────────────────────────────────────────────────────

_GITHUB_TTL_SEC = 6 * 3600  # refresh the verified identity at most this often


def _git_author(repo: Path, sha: str) -> str:
    """The commit's real author (``Name <email>``) — independent provenance."""
    try:
        out = _git(
            repo, "show", "-s", "--format=%an <%ae>", sha
        ).decode("utf-8", errors="replace").strip()
        return out
    except subprocess.CalledProcessError:
        return ""


def _github_identity(data_root: Path) -> Optional[str]:
    """Verified GitHub login via ``gh`` (the Aitherium GitHub OAuth-app identity).

    Best-effort and never load-bearing: returns None on any failure (no ``gh``
    on PATH, offline, unauthenticated) so capture never depends on the network.
    Cached in the vcs store for ``_GITHUB_TTL_SEC`` so per-commit hooks hit the
    network at most once per window. Phase 6+ makes this identity (via the
    Aitherium GitHub OAuth app for humans and the Aitherium GitHub app for the
    agent fleet) the authoritative actor source; the schema already records it
    via ``verified_actor`` / ``actor_verified``.
    """
    cache = data_root / "identity.json"
    if cache.exists():
        try:
            rec = json.loads(cache.read_text(encoding="utf-8"))
            if time.time() - rec.get("ts", 0) < _GITHUB_TTL_SEC and rec.get("login"):
                return rec["login"]
        except (OSError, ValueError):
            logger.debug("vcs: identity cache unreadable, re-resolving")
    try:
        out = subprocess.run(
            ["gh", "api", "user", "-q", ".login"],
            capture_output=True, text=True, encoding="utf-8",
            timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps({"ts": time.time(), "login": out}), encoding="utf-8"
        )
    except OSError:
        logger.debug("vcs: identity cache write failed (best-effort)")
    return out


def resolve_actor(
    actor_arg: Optional[str], repo: Path, git_sha: str, data_root: Path
) -> Dict[str, object]:
    """Resolve the claimed actor + verified identity for an op.

    Claimed-actor priority: explicit arg → ``AITHER_ACTOR`` env → verified
    GitHub login → commit author. ``actor_verified``/``verified_actor`` record
    the box's VERIFIED identity independently of the claimed actor, so a
    session claiming ``AITHER_ACTOR=lyra`` on a box verified as ``wizzense``
    records both — attribution stays honest without losing the agent name.
    """
    github = _github_identity(data_root)
    env_actor = os.environ.get("AITHER_ACTOR")
    # The SESSION, before the GitHub login. Every agent on this box shares one
    # GitHub identity, so resolving to `github` collapsed every session into one
    # actor: measured 2026-08-09, an op-log of 188 ops across 189 files had
    # exactly ONE actor, which makes "two actors touched this node" impossible to
    # express and the collision view structurally blind. The schema already
    # separates CLAIMED from VERIFIED — `verified_actor` still records the
    # GitHub identity below — so the claimed actor is free to be the session
    # that actually made the edit, which is what attribution is for. Matches
    # the lease gate's `_actor()`, and the two disagreeing is what made leases
    # discriminate per session while captures did not.
    session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if actor_arg:
        actor, source = actor_arg, "arg"
    elif env_actor:
        actor, source = env_actor, "env"
    elif session:
        actor, source = f"claude:{session}", "session"
    elif github:
        actor, source = github, "github"
    else:
        author = _git_author(repo, git_sha)
        actor, source = author or "unknown", "git-author" if author else "unknown"
    return {
        "actor": actor,
        "actor_source": source,
        "actor_verified": bool(github),
        "verified_actor": github or "",
    }


def _ancestor_shas(repo: Path, sha: str) -> List[str]:
    """First-parent chain of ``sha``, closest first, excluding ``sha``."""
    out = _git(repo, "rev-list", "--first-parent", sha).decode().split()
    return out[1:]  # first entry is sha itself


def _node_records(src: Optional[bytes], rel_path: str) -> List[Dict[str, Any]]:
    if src is None:
        return []
    if not rel_path.endswith(".py"):
        # NON-PYTHON: identity comes from repowise, which parses 75 extensions
        # across 20+ languages and emits ids of the form `path::qualified_name`
        # — verified stable across a symbol MOVING and its body changing, which
        # is the property node-level merge rests on. Without this every
        # .ts/.tsx/.go/.cs file was invisible to awgit: of the 20,451 files
        # repowise has indexed in this repo, 12,600+ are not Python.
        from awgit.repowise_parser import parse_symbols

        text = src.decode("utf-8", errors="ignore").split("\n")
        out: List[Dict[str, Any]] = []
        for sym in parse_symbols(src, rel_path):
            start, end = sym.get("start_line") or 0, sym.get("end_line") or 0
            body = "\n".join(text[start - 1:end]) if start and end else ""
            out.append({
                "name": sym["symbol"] or sym["node_id"],
                "path": rel_path,
                "type": sym.get("kind") or "symbol",
                "signature": "",
                "body": body,
            })
        return out

    # lazy: CodeGraph's import chain is ~15s (AitherConfig auto-tune etc.); only
    # pay it when actually parsing (capture/diff/merge runtime), never for
    # `vcs lease`/`status` which import this module but never parse.
    from awgit.parser import parse_source_bytes

    graph = parse_source_bytes(src, rel_path)
    lines = src.decode("utf-8", errors="ignore").split("\n")
    recs: List[Dict[str, Any]] = []
    for chunk in graph.chunks:
        body = ""
        if chunk.end_line:
            body = "\n".join(lines[chunk.start_line - 1: chunk.end_line])
        recs.append({
            "name": chunk.name,
            "path": rel_path,
            "type": chunk.chunk_type.value,
            "signature": chunk.signature or "",
            "body": body,
        })
    return recs


def _body_sha(rec: Dict[str, Any]) -> Optional[str]:
    body = rec.get("body") or ""
    return _blob_sha(body.encode("utf-8")) if body else None


def _diff_node_sets(
    old: List[Dict[str, Any]],
    new: List[Dict[str, Any]],
    manager: StableNodeIDManager,
    store: Optional[BodyStore] = None,
) -> List[NodeChange]:
    """Diff two node sets keyed by stable node id.

    Rename detection pairs genuinely-ADDED names with genuinely-DELETED names
    (same file, same type, similar body). A name present in BOTH sets is
    unchanged or rewritten — NEVER a rename candidate — which is what keeps a
    coincidentally-similar ADDED function from being misread as a rename (a
    tiny function's body is structurally confusable at ~0.75-0.8 similarity).
    The renamed node reuses the old stable id; the persisted legacy alias lets
    a divergent branch still resolve the pre-rename symbol to it (the M5
    rename gate).
    """
    old_by: Dict[str, Dict[str, Any]] = {
        manager.id_for(r["name"], r["path"], r["type"]): r for r in old
    }
    old_sid_by_name: Dict[str, str] = {}
    for sid, r in old_by.items():
        old_sid_by_name.setdefault(r["name"], sid)

    old_names = {r["name"] for r in old}
    new_names = {r["name"] for r in new}
    added_cands = [r for r in new if r["name"] not in old_names]
    deleted_cands = [r for r in old if r["name"] not in new_names]
    rename_pairs: List[tuple] = []
    used_old_names: set = set()
    for nr in added_cands:
        best_o, best_score = None, 0.0
        for o in deleted_cands:
            if o["name"] in used_old_names:
                continue
            if o["path"] != nr["path"] or o["type"] != nr["type"]:
                continue
            score = _similarity(o["body"], nr["body"])
            if score > best_score:
                best_o, best_score = o, score
        if best_o is not None and best_score >= _RENAME_SIMILARITY:
            used_old_names.add(best_o["name"])
            rename_pairs.append((best_o, nr))

    new_by: Dict[str, Dict[str, Any]] = {}
    for o, nr in rename_pairs:
        sid = old_sid_by_name[o["name"]]
        manager.rename_node(sid, nr["name"])
        new_by[sid] = nr
    used_new_names = {nr["name"] for _, nr in rename_pairs}
    for r in new:
        if r["name"] in used_new_names:
            continue
        new_by[manager.id_for(r["name"], r["path"], r["type"])] = r

    changes: List[NodeChange] = []
    for o, nr in rename_pairs:
        sid = old_sid_by_name[o["name"]]
        changes.append(NodeChange(
            node_id=sid,
            change_type="renamed",
            old_body_sha=_body_sha(o),
            new_body_sha=_body_sha(nr),
            symbol=nr["name"],
            path=nr["path"],
            renamed_from=o["name"],
        ))
    renamed_sids = {old_sid_by_name[o["name"]] for o, _ in rename_pairs}
    for sid in sorted(old_by.keys() & new_by.keys()):
        if sid in renamed_sids:
            continue  # consumed by a rename above
        o, n = old_by[sid], new_by[sid]
        osha, nsha = _body_sha(o), _body_sha(n)
        if osha == nsha:
            continue
        ctype = (
            "signature_changed"
            if o["signature"] != n["signature"]
            else "body_rewrite"
        )
        changes.append(NodeChange(
            node_id=sid,
            change_type=ctype,
            old_body_sha=osha,
            new_body_sha=nsha,
            symbol=n["name"],
            path=n["path"],
        ))
    for sid in sorted(new_by.keys() - old_by.keys()):
        n = new_by[sid]
        changes.append(NodeChange(
            node_id=sid,
            change_type="added",
            new_body_sha=_body_sha(n),
            symbol=n["name"],
            path=n["path"],
        ))
    for sid in sorted(old_by.keys() - new_by.keys()):
        if sid in renamed_sids:
            continue
        o = old_by[sid]
        changes.append(NodeChange(
            node_id=sid,
            change_type="deleted",
            old_body_sha=_body_sha(o),
            symbol=o["name"],
            path=o["path"],
        ))
    if store is not None:
        # Materialize the bodies the op references so the op-log is
        # self-contained — reconstructable WITHOUT git blobs (which live on the
        # failing D: drive). Content addressing dedupes: identical bodies (across
        # commits/branches/worktrees) collapse to one blob.
        bodies: Dict[str, bytes] = {}
        for r in list(old) + list(new):
            body = r.get("body")
            if body:
                b = body.encode("utf-8")
                bodies[_blob_sha(b)] = b
        for nc in changes:
            for sha in (nc.old_body_sha, nc.new_body_sha):
                if sha and sha in bodies:
                    store.put(bodies[sha])
    return changes


_RENAME_SIMILARITY = 0.75


def _similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def diff_sources(
    a_src: Optional[bytes],
    b_src: Optional[bytes],
    rel_path: str,
    manager: StableNodeIDManager,
    store: Optional[BodyStore] = None,
) -> List[NodeChange]:
    """Node-level diff of two source blobs (shared by capture and diff)."""
    return _diff_node_sets(
        _node_records(a_src, rel_path),
        _node_records(b_src, rel_path),
        manager,
        store,
    )


def load_node_manager(data_root: Path) -> "StableNodeIDManager":
    """Read-only stable-id manager over a persisted registry, if one exists.

    ``persist=False`` + explicit ``_load`` keeps a pure diff from WRITING the
    registry — unseen symbols get ephemeral ids for the call without being
    registered (a diff must not mutate the node universe).
    """
    from awgit.nodeid import StableNodeIDManager

    path = data_root / "nodes.json"
    if path.exists():
        mgr = StableNodeIDManager(path=path, persist=False)
        mgr._load()  # init skips the load when persist=False
        return mgr
    return StableNodeIDManager()


def _make_summary(actor: str, changes: List[NodeChange]) -> str:
    counts: Dict[str, int] = {}
    for c in changes:
        counts[c.change_type] = counts.get(c.change_type, 0) + 1
    parts = [f"{counts[k]} {k}" for k in sorted(counts)]
    who = actor or "unknown"
    return f"{who}: {', '.join(parts)}" if parts else f"{who}: no semantic changes"


def capture_ops(
    git_sha: str,
    *,
    actor: Optional[str] = None,
    repo_path: Optional[str] = None,
    root_path: Optional[str] = None,
    data_root: Optional[Path] = None,
) -> Optional[EditOp]:
    """Capture ``git_sha`` as an ``EditOp`` (idempotent by commit).

    Returns None when there is nothing to record (root commit, no Python
    semantic changes) or the op already exists. Serialized by a per-store
    file lock so concurrent post-commit hooks cannot tear the node registry
    or double-log a commit.
    """
    repo = Path(repo_path or default_repo())
    data = data_root or vcs_data_root(root_path=root_path)

    parent = git_parent(repo, git_sha)
    if parent is None:
        return None
    files = git_changed_files(repo, git_sha)
    if not files:
        return None

    # Provenance is resolved BEFORE the lock: the gh identity cache is a
    # separate file, and the lock must never be held across a network call.
    prov = resolve_actor(actor, repo, git_sha, data)
    actor_name = str(prov["actor"])

    data.mkdir(parents=True, exist_ok=True)
    with FileLock(data / "capture.lock"):
        from awgit.nodeid import StableNodeIDManager

        oplog = OpLog(data_root=data)
        if oplog.has_commit(git_sha):
            return oplog.ops_for_commit(git_sha)[0]

        manager = StableNodeIDManager(path=data / "nodes.json", persist=True)
        store = BodyStore(data_root=data)
        node_changes: List[NodeChange] = []
        from awgit.repowise_parser import language_for

        for rel in files:
            # Python natively; anything else only when repowise can parse it,
            # so a file type nobody can give node identity to is skipped rather
            # than recorded as an empty change.
            if not rel.endswith(".py") and not language_for(rel):
                continue
            old_src = git_blob(repo, parent, rel)
            new_src = git_blob(repo, git_sha, rel)
            node_changes.extend(diff_sources(old_src, new_src, rel, manager, store))
        if not node_changes:
            return None

        ts = datetime.now(timezone.utc).isoformat()
        sha_to_op = oplog.sha_index()
        parent_ops = [
            sha_to_op[a] for a in _ancestor_shas(repo, git_sha) if a in sha_to_op
        ]
        op_id = os.urandom(16).hex()
        op = EditOp(
            op_id=op_id,
            parent_ops=parent_ops,
            actor=actor_name,
            ts=ts,
            git_sha=git_sha,
            git_parent_sha=parent,
            file_paths=files,
            node_changes=node_changes,
            summary=_make_summary(actor_name, node_changes),
            leased=False,
            actor_verified=bool(prov["actor_verified"]),
            actor_source=str(prov["actor_source"]),
            verified_actor=str(prov["verified_actor"]),
            ledger_ref=mint_ledger_ref(op_id, git_sha),
        )
        oplog.append(op)
        # a self-capture is by definition "applied" on this node — so
        # sync_status shows it as covered, not missing.
        from awgit.sync import mark_op_applied

        mark_op_applied(op.op_id, data_root=data)
        return op
