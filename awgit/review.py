"""Review threads anchored to a NODE, so they survive what rebases them.

A review comment on GitHub is anchored to a (commit, file, line). Rebase the
branch and the line moves; reformat the file and it moves further; move the
function to another file and the anchor is gone. The thread becomes "outdated",
collapses, and the objection it recorded quietly stops being in front of anyone.

garden fixes half of this by anchoring to a COMMIT rather than a diff position,
so a rebase does not orphan the thread. awgit anchors to a **node id**, which
survives the rebase and the reformat outright, and a rename within a file through
the redirect capture registers.

A cross-FILE move needs one more step, and it is worth being exact about rather
than hand-waving: the node id is keyed on (name, path) and capture's rename
detection is scoped to one file, so moving a function elsewhere mints a new id.
:func:`locate` closes that by falling back to (name, type) across the files it
is given — and REFUSES when two candidates match, because silently attaching a
reviewer's objection to a different function reads as answered.

The line number is therefore not stored. It is COMPUTED at display and at post
time from where the node is now. A stored line is a fact that expires; a node id
is a fact that does not.

Comments are DRAFTS until ``submit``, so you can read a whole diff and publish
once rather than dribbling notifications. And a thread whose node has been
DELETED is shown as orphaned rather than dropped: "the function you objected to
is gone" is an outcome a reviewer must see, not a reason to hide the objection.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


@dataclass
class Comment:
    author: str
    body: str
    created_at: str
    draft: bool = True
    gh_id: Optional[int] = None


@dataclass
class Thread:
    """A conversation about ONE node."""

    thread_id: str
    node_id: str
    change_id: str
    symbol: str
    path: str          # where the node was when the thread opened — a HINT only
    comments: List[Comment] = field(default_factory=list)
    resolved: bool = False

    def to_dict(self) -> dict:
        out = asdict(self)
        out["comments"] = [asdict(c) for c in self.comments]
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Thread":
        comments = [Comment(**c) for c in d.get("comments", [])]
        return cls(
            thread_id=d["thread_id"], node_id=d["node_id"],
            change_id=d.get("change_id", ""), symbol=d.get("symbol", ""),
            path=d.get("path", ""), comments=comments,
            resolved=bool(d.get("resolved")),
        )

    @property
    def open(self) -> bool:
        return not self.resolved


def _store_dir(data_root: Optional[Path] = None) -> Path:
    from awgit.data_root import vcs_data_root

    root = (data_root or vcs_data_root()) / "reviews"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path_for(change_id: str, data_root: Optional[Path] = None) -> Path:
    safe = change_id.replace("/", "_") or "unknown"
    return _store_dir(data_root) / f"{safe}.json"


def load(change_id: str, data_root: Optional[Path] = None) -> List[Thread]:
    path = _path_for(change_id, data_root)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [Thread.from_dict(t) for t in raw.get("threads", [])]


def save(change_id: str, threads: List[Thread],
         data_root: Optional[Path] = None) -> None:
    path = _path_for(change_id, data_root)
    payload = {"change_id": change_id, "threads": [t.to_dict() for t in threads]}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def add_comment(change_id: str, node_id: str, body: str, author: str,
                symbol: str = "", path: str = "",
                data_root: Optional[Path] = None) -> Thread:
    """Append to the thread for this node, opening one if needed."""
    threads = load(change_id, data_root)
    for thread in threads:
        if thread.node_id == node_id:
            thread.comments.append(Comment(author=author, body=body,
                                           created_at=_now()))
            save(change_id, threads, data_root)
            return thread
    thread = Thread(thread_id=uuid.uuid4().hex, node_id=node_id,
                    change_id=change_id, symbol=symbol, path=path,
                    comments=[Comment(author=author, body=body, created_at=_now())])
    threads.append(thread)
    save(change_id, threads, data_root)
    return thread


def resolve(change_id: str, thread_id: str,
            data_root: Optional[Path] = None) -> bool:
    threads = load(change_id, data_root)
    for thread in threads:
        if thread.thread_id.startswith(thread_id):
            thread.resolved = True
            save(change_id, threads, data_root)
            return True
    return False


def submit(change_id: str, data_root: Optional[Path] = None) -> List[Comment]:
    """Mark every draft published; returns the ones that just went out."""
    threads = load(change_id, data_root)
    published = []
    for thread in threads:
        for comment in thread.comments:
            if comment.draft:
                comment.draft = False
                published.append(comment)
    save(change_id, threads, data_root)
    return published


def unresolved(change_id: str, data_root: Optional[Path] = None) -> List[Thread]:
    """Open threads. These BLOCK a merge — see `awgit pr merge`."""
    return [t for t in load(change_id, data_root) if t.open]


# ── locating a node NOW, which is the whole point ────────────────────────

def locate(node_id: str, hint_path: str = "",
           repo: Optional[Path] = None,
           search: Optional[List[str]] = None) -> Optional[Tuple[str, int]]:
    """Where is this node *now*? ``(path, start_line)`` or None if it is gone.

    The hint is tried first because a node usually has not moved, and parsing
    one file is cheap. When it is not there the search widens — that is the case
    the whole design exists for: the function moved to another file, and a
    line-anchored comment would already have been lost.

    Returning None is a real answer, not a failure: the node was DELETED, and
    the caller shows the thread as orphaned rather than dropping it.
    """
    from awgit.capture import _node_records, load_node_manager
    from awgit.data_root import vcs_data_root

    manager = load_node_manager(vcs_data_root())
    base = Path(repo or ".")
    candidates: List[str] = []
    if hint_path:
        candidates.append(hint_path)
    for extra in search or []:
        if extra not in candidates:
            candidates.append(extra)

    want = manager.resolve_node_id(node_id)
    known = manager.get_node(want) or manager.get_node(node_id) or {}
    by_name: List[Tuple[str, int]] = []

    for rel in candidates:
        target = base / rel
        try:
            src = target.read_bytes()
        except OSError:
            continue
        try:
            records = _node_records(src, rel)
        except Exception:  # noqa: BLE001 - unparseable now; not this node's fault
            continue
        for record in records:
            here = manager.id_for(record["name"], record["path"], record["type"])
            # resolve_node_id follows the redirects capture registers for a
            # rename WITHIN a file. Comparing raw ids alone would report a moved
            # node as deleted — a silent "orphaned" thread.
            if manager.resolve_node_id(here) == want:
                return rel, int(record.get("start_line") or 1)
            if (known and record["name"] == known.get("name")
                    and record["type"] == known.get("type", record["type"])):
                by_name.append((rel, int(record.get("start_line") or 1)))

    # A CROSS-FILE move. The node id is keyed on (name, path), and capture's
    # rename detection is deliberately scoped to one file — so moving a function
    # to another file produces a delete plus an add, with different ids, and an
    # id-only lookup reports the thread as orphaned. The thread is not orphaned;
    # the function is right there in the next file.
    #
    # Falling back to (name, type) across the SEARCHED files closes that, and
    # ambiguity is refused rather than guessed: two same-named functions in the
    # candidate set means the wrong one would silently inherit the objection,
    # which is worse than saying nothing.
    if len(by_name) == 1:
        return by_name[0]
    return None


def render(threads: List[Thread], repo: Optional[Path] = None,
           search: Optional[List[str]] = None) -> List[str]:
    """Threads with their CURRENT location resolved, orphans marked."""
    lines: List[str] = []
    for thread in threads:
        where = locate(thread.node_id, thread.path, repo, search)
        if where is None:
            head = (f"  [orphaned] {thread.symbol or thread.node_id[:12]} — the node "
                    f"is gone (was {thread.path})")
        else:
            path, line = where
            head = f"  {path}:{line}  {thread.symbol or thread.node_id[:12]}"
        state = "resolved" if thread.resolved else "OPEN"
        lines.append(f"{head}   [{state} {thread.thread_id[:8]}]")
        for comment in thread.comments:
            mark = "draft" if comment.draft else "sent"
            lines.append(f"      ({mark}) {comment.author}: {comment.body[:70]}")
    if not threads:
        lines.append("  no review threads")
    return lines


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
