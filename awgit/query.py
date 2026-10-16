"""Read-only repository queries — ask the repository, don't check it out.

An agent that needs "who wrote line 40", "what did this file look like at
develop" or "do these two branches collide" should not need a working copy: a
checkout is minutes of disk and a shared-tree hazard, the question is one git
plumbing call. These handlers are those calls, shaped as JSON-able dicts for
the MCP surface (``awgit.mcp.TOOLS``, which a gateway mounts like the other
``vcs_*`` tools).

THE RULES THAT MAKE THEM SAFE TO EXPOSE
---------------------------------------
* **The repository is the process's, never the caller's.** ``VCS_REPO_ROOT``
  (server config) or the git top-level of the working directory. No handler
  takes a repo argument; which tree gets read is not the model's call.
* **Paths cannot escape it.** Absolute paths, drive letters, ``..`` segments,
  pathspec magic (a leading ``:``) and anything starting with ``-`` are refused,
  and every path is passed after ``--``.
* **Revs are resolved before use.** A rev starting with ``-`` is refused (it
  would become a git option), the rest go through ``git rev-parse --verify``
  and only the resulting full sha is ever handed to a later git call.
* **Output is capped and says so.** Every text result carries ``truncated``.

Failures come back as ``{"error": ...}`` dicts, never as exceptions: an MCP
client reads a structured refusal; a traceback is noise.
"""

from __future__ import annotations

import codecs
import functools
import os
import re
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

#: Default and hard ceiling for any single text payload (file body, diff).
DEFAULT_MAX_BYTES = 200_000
HARD_MAX_BYTES = 1_000_000
#: Blame lines returned before truncation (a 10k-line file's blame is ~2 MB).
MAX_BLAME_LINES = 2_000
MAX_LOG_LIMIT = 200
#: Files per side a merge preview will parse. Each is two blob reads + a parse.
MAX_PREVIEW_FILES = 300
GIT_TIMEOUT_SEC = 60

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DRIVE = re.compile(r"^[A-Za-z]:")
#: Ranges (``..``), blob specs (``:``), globs and whitespace are not one commit.
_BAD_REV_CHARS = re.compile(r"[\s\x00-\x1f\x7f:?*\[\\]|\.\.")


class QueryError(Exception):
    """A refusal or failure, returned to the caller as ``{"error": msg}``."""


def _structured(fn: Callable[..., Dict[str, Any]]) -> Callable[..., Dict[str, Any]]:
    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except QueryError as exc:
            return {"error": str(exc), "tool": fn.__name__}

    return _wrapped


# ── repository + git plumbing ─────────────────────────────────────────────

def _git_argv(repo: Path, args: Any) -> List[str]:
    """``git`` with the config every query needs, whatever the host's config says.

    ``core.quotePath=false``: otherwise a non-ASCII path comes back as
    ``"caf\\303\\251.py"`` -- quoted and octal-escaped -- and stops matching
    anything (a merge preview skipped such a file and reported ``clean``).
    ``safe.directory``: a gateway container reading a bind-mounted repository
    owned by another uid is refused by git's ownership check; the repository
    is server configuration, so trusting exactly that one path is correct.
    """
    return ["git", "-c", "core.quotePath=false",
            "-c", f"safe.directory={repo.as_posix()}", *args]


def _decode(raw: Optional[bytes]) -> str:
    """UTF-8, no newline translation: a CR in the repository is a CR in the answer."""
    return (raw or b"").decode("utf-8", errors="replace")


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git and return ``stdout``/``stderr`` as text decoded from BYTES.

    Not ``text=True``: that turns on universal newlines, and ``a\\r\\nb`` came
    back as ``a\\nb`` -- a blame or a file body that silently differed from
    the blob on every CRLF file in a Windows repository.
    """
    try:
        proc = subprocess.run(
            _git_argv(repo, args), cwd=str(repo), capture_output=True,
            timeout=GIT_TIMEOUT_SEC, creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise QueryError("git is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise QueryError(f"git {args[0]} timed out after {GIT_TIMEOUT_SEC}s") from exc
    proc.stdout, proc.stderr = _decode(proc.stdout), _decode(proc.stderr)
    if check and proc.returncode != 0:
        raise QueryError(f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
    return proc


def _git_capped_bytes(repo: Path, args: List[str], max_bytes: int) -> Tuple[bytes, bool]:
    """Stream git's stdout, stopping at ``max_bytes`` BYTES. -> (raw, truncated).

    ``run(capture_output=True)`` would buffer a multi-GB diff in memory before
    the cap could apply; reading at most ``max_bytes + 1`` and killing the
    child bounds the cost by the cap, not by the repository. A watchdog kills
    the child after ``GIT_TIMEOUT_SEC``: ``wait(timeout=)`` alone came too
    late, after a ``read`` that could block forever on a stalled git.
    """
    try:
        proc = subprocess.Popen(
            _git_argv(repo, args), cwd=str(repo), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise QueryError("git is not on PATH") from exc
    expired = threading.Event()

    def _expire() -> None:
        expired.set()
        proc.kill()

    watchdog = threading.Timer(GIT_TIMEOUT_SEC, _expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        assert proc.stdout is not None
        body = proc.stdout.read(max_bytes + 1)
        truncated = len(body) > max_bytes
        if truncated:
            proc.kill()
            body = body[:max_bytes]
        proc.stdout.close()
        rc = proc.wait()
    finally:
        watchdog.cancel()
    if expired.is_set():
        raise QueryError(f"git {args[0]} timed out after {GIT_TIMEOUT_SEC}s")
    if rc != 0 and not truncated:
        raise QueryError(f"git {args[0]} exited {rc}")
    return body, truncated


def _git_capped(repo: Path, args: List[str], max_bytes: int) -> Tuple[str, bool]:
    """``_git_capped_bytes`` decoded once. A cut mid-character drops the partial
    character rather than emitting a replacement, so the text never exceeds
    ``max_bytes`` UTF-8 bytes."""
    raw, truncated = _git_capped_bytes(repo, args, max_bytes)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    return decoder.decode(raw, final=not truncated), truncated


def repo_root() -> Path:
    """The repository these queries read: server config, never caller input.

    ``VCS_REPO_ROOT`` may name a working tree or a BARE git directory: a
    container needs only a read-only mount of ``.git``, not a checkout.
    """
    configured = os.environ.get("VCS_REPO_ROOT")
    start = Path(configured).resolve() if configured else Path.cwd()
    if not start.is_dir():
        raise QueryError(f"configured repository {str(start)!r} does not exist")
    bare = _git(start, "rev-parse", "--is-bare-repository", check=False)
    if bare.returncode == 0 and bare.stdout.strip() == "true":
        gitdir = _git(start, "rev-parse", "--absolute-git-dir", check=False)
        if gitdir.returncode == 0 and gitdir.stdout.strip():
            return Path(gitdir.stdout.strip())
    proc = _git(start, "rev-parse", "--show-toplevel", check=False)
    top = proc.stdout.strip()
    if proc.returncode != 0 or not top:
        where = "VCS_REPO_ROOT" if configured else "the server's working directory"
        raise QueryError(
            f"this server has no git repository to query ({where} {str(start)!r} is "
            "not one; set VCS_REPO_ROOT on the server to a checkout or a .git "
            "directory -- it is never a tool argument)"
        )
    return Path(top)


def _rev(repo: Path, rev: Any, field: str = "rev") -> str:
    """Resolve ``rev`` to a full commit sha, refusing option injection."""
    text = str(rev if rev is not None else "").strip()
    if not text:
        raise QueryError(f"{field} is required")
    if text.startswith("-"):
        raise QueryError(f"{field} {text!r} starts with '-' and would be read as a git option")
    if len(text) > 256 or _BAD_REV_CHARS.search(text):
        raise QueryError(f"{field} {text!r} is not a plain ref or sha")
    proc = _git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
                f"{text}^{{commit}}", check=False)
    sha = (proc.stdout or "").strip()
    if proc.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", sha):
        raise QueryError(f"{field} {text!r} does not name a commit in this repository")
    return sha


def _path(repo: Path, path: Any, field: str = "path") -> str:
    """A repo-relative POSIX path that provably stays inside ``repo``."""
    text = str(path if path is not None else "").strip().replace("\\", "/")
    if not text:
        raise QueryError(f"{field} is required")
    if "\x00" in text:
        raise QueryError(f"{field} contains a NUL byte")
    if text.startswith("/") or _DRIVE.match(text):
        raise QueryError(f"{field} {text!r} is absolute; give a path relative to the repository")
    if text.startswith(("-", ":")):
        raise QueryError(f"{field} {text!r} would be read as a git option or pathspec magic")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise QueryError(f"{field} {text!r} escapes the repository")
    rel = "/".join(parts)
    root = repo.resolve()
    full = (root / rel).resolve()
    if full != root and root not in full.parents:
        raise QueryError(f"{field} {text!r} escapes the repository")
    return rel


def _cap(max_bytes: Any) -> int:
    try:
        n = int(max_bytes)
    except (TypeError, ValueError):
        n = DEFAULT_MAX_BYTES
    return max(1, min(HARD_MAX_BYTES, n))


def _line(value: Any, field: str) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise QueryError(f"{field} must be an integer") from exc
    if n < 1:
        raise QueryError(f"{field} must be >= 1")
    return n


# ── the tools ─────────────────────────────────────────────────────────────

def _parse_porcelain(text: str) -> List[Dict[str, Any]]:
    """``git blame --porcelain`` -> one dict per line (commit headers deduped by git)."""
    commits: Dict[str, Dict[str, Any]] = {}
    out: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for raw in text.split("\n"):
        if raw.startswith("\t"):
            if cur is not None:
                meta = commits.get(cur["sha"], {})
                out.append({**cur, "author": meta.get("author", ""),
                            "author_mail": meta.get("author-mail", ""),
                            "author_time": meta.get("author-time", ""),
                            "summary": meta.get("summary", ""), "content": raw[1:]})
            cur = None
            continue
        head = raw.split(" ")
        if len(head) >= 3 and re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", head[0]):
            cur = {"sha": head[0], "orig_line": int(head[1]), "line": int(head[2])}
            commits.setdefault(head[0], {})
        elif cur is not None and " " in raw:
            key, _, val = raw.partition(" ")
            if key in ("author", "author-mail", "author-time", "summary"):
                commits[cur["sha"]][key] = val
    return out


@_structured
def vcs_blame(path: str, rev: str = "HEAD", start_line: Optional[int] = None,
              end_line: Optional[int] = None, **_kw) -> Dict[str, Any]:
    """Line-by-line authorship of ``path`` at ``rev`` (optionally a line range)."""
    repo = repo_root()
    rel, sha = _path(repo, path), _rev(repo, rev)
    lo, hi = _line(start_line, "start_line"), _line(end_line, "end_line")
    args = ["blame", "--porcelain"]
    if lo or hi:
        if lo and hi and hi < lo:
            raise QueryError("end_line is before start_line")
        args += ["-L", f"{lo or 1},{hi or ''}"]
    proc = _git(repo, *args, sha, "--", rel, check=False)
    if proc.returncode != 0:
        raise QueryError(f"blame failed: {(proc.stderr or '').strip()[:300]}")
    lines = _parse_porcelain(proc.stdout)
    truncated = len(lines) > MAX_BLAME_LINES
    return {"path": rel, "rev": sha, "count": min(len(lines), MAX_BLAME_LINES),
            "lines": lines[:MAX_BLAME_LINES], "truncated": truncated}


@_structured
def vcs_path_log(path: str, rev: str = "HEAD", limit: int = 20, **_kw) -> Dict[str, Any]:
    """Commits that touched ``path`` reachable from ``rev``, newest first.

    Plain ``git log <sha> -- <path>``: with a commit-graph written, git answers
    this from the changed-path Bloom filters instead of diffing every commit.
    """
    repo = repo_root()
    rel, sha = _path(repo, path), _rev(repo, rev)
    try:
        n = max(1, min(MAX_LOG_LIMIT, int(limit)))
    except (TypeError, ValueError):
        n = 20
    fmt = "%H%x1f%an%x1f%ae%x1f%aI%x1f%s"
    proc = _git(repo, "log", f"--format={fmt}", "-n", str(n + 1), sha, "--", rel)
    commits = []
    # Split on LF only: splitlines() also breaks on \r, \x1c-\x1e and \x85,
    # all of which a commit subject can carry.
    for ln in proc.stdout.split("\n"):
        f = ln.split("\x1f")
        if len(f) == 5:
            commits.append({"sha": f[0], "author": f[1], "email": f[2],
                            "date": f[3], "subject": f[4]})
    return {"path": rel, "rev": sha, "count": min(len(commits), n),
            "commits": commits[:n], "truncated": len(commits) > n}


@_structured
def vcs_merge_base(a: str, b: str, **_kw) -> Dict[str, Any]:
    """Best common ancestor of two commits (``None`` when histories are unrelated)."""
    repo = repo_root()
    sa, sb = _rev(repo, a, "a"), _rev(repo, b, "b")
    proc = _git(repo, "merge-base", sa, sb, check=False)
    if proc.returncode not in (0, 1):
        raise QueryError(f"merge-base failed: {(proc.stderr or '').strip()[:300]}")
    base = (proc.stdout or "").strip() or None
    return {"a": sa, "b": sb, "merge_base": base,
            "a_is_ancestor": base == sa, "b_is_ancestor": base == sb}


@_structured
def vcs_file_at(path: str, rev: str = "HEAD", max_bytes: int = DEFAULT_MAX_BYTES,
                **_kw) -> Dict[str, Any]:
    """The content of ``path`` at ``rev``, without touching any working tree."""
    repo = repo_root()
    rel, sha = _path(repo, path), _rev(repo, rev)
    cap = _cap(max_bytes)
    spec = f"{sha}:{rel}"
    kind = _git(repo, "cat-file", "-t", spec, check=False)
    if kind.returncode != 0:
        raise QueryError(f"{rel!r} does not exist at {sha[:12]}")
    if kind.stdout.strip() != "blob":
        raise QueryError(f"{rel!r} is a {kind.stdout.strip()} at {sha[:12]}, not a file")
    size = int(_git(repo, "cat-file", "-s", spec).stdout.strip() or 0)
    raw, truncated = _git_capped_bytes(repo, ["cat-file", "blob", spec], cap)
    binary = b"\x00" in raw[:8192]
    if binary:
        return {"path": rel, "rev": sha, "size": size, "binary": True, "content": "",
                "lossy": False, "truncated": False}
    # Decoded once from the blob's bytes: CRLF stays CRLF. A cut mid-character
    # drops the partial character instead of inventing one.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    try:
        body, lossy = decoder.decode(raw, final=not truncated), False
    except UnicodeDecodeError:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        body, lossy = decoder.decode(raw, final=not truncated), True
    # ``lossy``: the blob is not UTF-8 and ``content`` carries U+FFFD in place
    # of the bytes it could not decode -- do not write it back as the file.
    return {"path": rel, "rev": sha, "size": size, "binary": False, "content": body,
            "lossy": lossy, "truncated": truncated}


def _parse_numstat_z(text: str) -> List[Dict[str, Any]]:
    """``git diff --numstat -z``: paths verbatim (never quoted), renames as from/to.

    A record is ``added\\tdeleted\\tpath\\0``, or for a rename
    ``added\\tdeleted\\t\\0from\\0to\\0``. Anything after the last NUL of a
    truncated stream is a partial record and is dropped.
    """
    tokens = text.split("\x00")
    tokens.pop()  # "" after the final NUL, or a partial record when truncated
    files: List[Dict[str, Any]] = []
    i = 0
    while i < len(tokens):
        f = tokens[i].split("\t", 2)
        i += 1
        if len(f) != 3:
            continue
        entry: Dict[str, Any] = {"path": f[2], "binary": f[0] == "-",
                                 "added": None if f[0] == "-" else int(f[0]),
                                 "deleted": None if f[1] == "-" else int(f[1])}
        if f[2] == "":
            if i + 1 >= len(tokens):
                break  # the rename's from/to were cut off by the cap
            entry["from"], entry["path"] = tokens[i], tokens[i + 1]
            i += 2
        files.append(entry)
    return files


@_structured
def vcs_diff(a: str, b: str, paths: Optional[List[str]] = None, stat_only: bool = False,
             max_bytes: int = DEFAULT_MAX_BYTES, **_kw) -> Dict[str, Any]:
    """Unified diff (or per-file numstat) between two commits, optionally path-limited."""
    repo = repo_root()
    sa, sb = _rev(repo, a, "a"), _rev(repo, b, "b")
    items = paths if isinstance(paths, (list, tuple)) else ([paths] if paths else [])
    rels = [_path(repo, p, "paths[]") for p in items]
    base = ["diff", "--no-color", "--no-ext-diff", "--no-textconv"]
    out: Dict[str, Any] = {"a": sa, "b": sb, "paths": rels}
    if stat_only in (True, "true", "True", 1, "1"):
        text, truncated = _git_capped(repo, [*base, "--numstat", "-z", sa, sb, "--", *rels],
                                      _cap(max_bytes))
        out.update(files=_parse_numstat_z(text), truncated=truncated)
        out["count"] = len(out["files"])
        return out
    text, truncated = _git_capped(repo, [*base, sa, sb, "--", *rels], _cap(max_bytes))
    out.update(diff=text, truncated=truncated)
    return out


def _side_op(repo: Path, base: str, tip: str, manager: Any) -> Tuple[Any, bool]:
    """An in-memory ``EditOp`` for base..tip. Nothing is written to any store."""
    from awgit.capture import diff_sources, git_blob
    from awgit.schema import EditOp

    # -z: names verbatim. Quoted, a non-ASCII ``"caf\303\251.py"`` failed the
    # .py test below and a real collision previewed as clean.
    names = _git(repo, "diff", "--name-only", "-z", "--no-renames", base, tip).stdout
    files = [n for n in names.split("\x00") if n]
    truncated = len(files) > MAX_PREVIEW_FILES
    changes = []
    for rel in files[:MAX_PREVIEW_FILES]:
        if not rel.endswith(".py"):
            continue
        changes.extend(diff_sources(git_blob(repo, base, rel), git_blob(repo, tip, rel),
                                    rel, manager))
    op = EditOp(op_id=f"preview-{tip[:12]}", parent_ops=[], actor="merge-preview",
                ts=datetime.now(timezone.utc).isoformat(), git_sha=tip,
                git_parent_sha=base, file_paths=files, node_changes=changes)
    return op, truncated


@_structured
def vcs_merge_preview(a: str, b: str, **_kw) -> Dict[str, Any]:
    """Node-level merge preview of two commits, over everything since their merge base.

    Wraps ``awgit.merge.merge_ops``: each side becomes an in-memory op set
    (``merge-base..a`` and ``merge-base..b``) over ONE read-only node registry,
    so the same symbol gets the same id on both sides. Conflicts ``merge_ops``
    would escalate are written to a throwaway store: a preview must not file
    entries in the real conflict inbox.
    """
    from awgit.capture import load_node_manager
    from awgit.data_root import vcs_data_root
    from awgit.merge import merge_ops

    repo = repo_root()
    sa, sb = _rev(repo, a, "a"), _rev(repo, b, "b")
    mb = _git(repo, "merge-base", sa, sb, check=False).stdout.strip()
    if not mb:
        raise QueryError("a and b share no history; there is nothing to preview")
    manager = load_node_manager(vcs_data_root())
    op_a, trunc_a = _side_op(repo, mb, sa, manager)
    op_b, trunc_b = _side_op(repo, mb, sb, manager)
    with tempfile.TemporaryDirectory(prefix="awgit-preview-") as scratch:
        result = merge_ops([op_a] if op_a.node_changes else [],
                           [op_b] if op_b.node_changes else [],
                           repo_path=str(repo), data_root=Path(scratch), enrich=False)
    return {"a": sa, "b": sb, "merge_base": mb, "status": result.status,
            "merged_node_ids": list(result.merged_node_ids),
            "conflicts": [c.to_dict() for c in result.conflicts],
            "notes": list(result.notes),
            "files": {"a": len(op_a.file_paths), "b": len(op_b.file_paths)},
            "truncated": trunc_a or trunc_b}


QUERY_TOOLS = {
    "vcs_blame": (vcs_blame, "Line authorship of a file at a rev (read-only, no checkout)"),
    "vcs_path_log": (vcs_path_log, "Commits that touched a path, newest first"),
    "vcs_merge_base": (vcs_merge_base, "Best common ancestor of two revs"),
    "vcs_file_at": (vcs_file_at, "A file's content at a rev, without a checkout"),
    "vcs_diff": (vcs_diff, "Unified diff or numstat between two revs, optionally per path"),
    "vcs_merge_preview": (vcs_merge_preview,
                          "Node-level merge preview of two revs since their merge base"),
}
