"""An MCP server for awgit — leases and safe commits as tools, over stdio.

Point a client at `awgit mcp`:

    {"mcpServers": {"awgit": {"command": "awgit", "args": ["mcp"]}}}

Tools: ``lease_acquire``, ``lease_release``, ``lease_list``, ``status``,
``fresh``, ``read``, ``blob_commit``.

WHY THIS EXISTS
---------------
A coding agent in a shared checkout reaches awgit through its shell, which means
every lease and every commit is a string it composes, quotes and parses back.
The dangerous mistakes are string mistakes: a missing path list, a path that
does not exist (which blob-commit records as a DELETION), a ref that begins with
``-`` and becomes a flag. As tools with typed arguments, those are refused
before anything runs.

DESIGN NOTES THAT MATTER
------------------------
**Every tool runs the real CLI** (``python -m awgit ...``) in a child process,
with stdin closed. The refusals the CLI already makes (lease conflicts, stale
copies, a moved branch under ``--advance``, a shrinking file) are therefore the
same refusals here, by construction rather than by a second copy of the rules.
A child process also keeps git's and awgit's own output off this server's
stdout, which IS the protocol stream.

**This server adds refusals, never removes one.** ``blob_commit`` refuses: no
paths (never a pathspec-less commit), a path that is missing on disk (it would
become a deletion), a directory, a path outside the repository, anything that
starts with ``-``, an empty message or base, and ``advance`` without a
``branch``. Deletions and ``--untrack`` stay CLI-only, where they are typed on
purpose.

**The repository is fixed at start** (the server's working directory), never a
tool argument: a tool argument is caller-suppliable, and which checkout gets
committed to is not the model's call to redirect.

**No SDK dependency.** The stdio transport is newline-delimited JSON-RPC 2.0 and
this module speaks the four methods a tools-only server needs (``initialize``,
``tools/list``, ``tools/call``, ``ping``). The Python SDK's 1.x and 2.x surfaces
are incompatible, and a server pinned to one fails to start where the other is
installed; a server with no dependency starts wherever awgit does.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

PROTOCOL_VERSION = "2025-06-18"
#: A read larger than this is truncated (and says so) rather than flooding context.
READ_MAX_BYTES = 256 * 1024
#: blob-commit on a large tree can take minutes; reads and leases should not.
TIMEOUT_SLOW = 600
TIMEOUT_FAST = 120


class RefusedError(ValueError):
    """An argument this server will not pass to the CLI. Never partially runs."""


def _repo_root() -> Path:
    return Path(os.getcwd())


def _run(argv: List[str], timeout: int = TIMEOUT_FAST) -> Dict[str, Any]:
    """Run ``python -m awgit <argv>`` in the repo; return a structured result."""
    try:
        p = subprocess.run(
            [sys.executable, "-m", "awgit", *argv], cwd=str(_repo_root()),
            stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "stdout": "",
                "stderr": f"awgit {argv[0]} timed out after {timeout}s"}
    return {"ok": p.returncode == 0, "exit_code": p.returncode,
            "stdout": p.stdout.decode("utf-8", "replace"),
            "stderr": p.stderr.decode("utf-8", "replace")}


def _no_flag(value: str, what: str) -> str:
    value = (value or "").strip()
    if not value:
        raise RefusedError(f"{what} is required")
    if value.startswith("-"):
        raise RefusedError(f"{what} {value!r} starts with '-' and would be read as a flag")
    return value


def _paths(paths: Any, what: str = "paths") -> List[str]:
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not paths:
        raise RefusedError(f"{what}: name at least one path — a pathspec-less operation "
                      "sweeps whatever peers have in flight")
    return [_no_flag(str(p), "path") for p in paths]


def _inside_repo(path: str) -> Path:
    root = _repo_root().resolve()
    full = (root / path).resolve()
    if full != root and root not in full.parents:
        raise RefusedError(f"path {path!r} is outside the repository")
    return full


def _actor_args(actor: Optional[str]) -> List[str]:
    return ["--actor", _no_flag(actor, "actor")] if actor else []


# ── tools ────────────────────────────────────────────────────────────────────

def lease_acquire(paths: Any, ttl_sec: int = 300, reason: str = "",
                  actor: Optional[str] = None) -> Dict[str, Any]:
    names = _paths(paths)
    for n in names:
        _inside_repo(n)
    ttl = max(1, min(3600, int(ttl_sec)))
    return _run(["lease", "acquire", "--ttl", str(ttl), f"--reason={reason}",
                 *_actor_args(actor), "--", *names])


def lease_release(targets: Any, actor: Optional[str] = None) -> Dict[str, Any]:
    return _run(["lease", "release", *_actor_args(actor), "--",
                 *_paths(targets, "targets")])


def lease_list() -> Dict[str, Any]:
    res = _run(["lease", "list", "--json"])
    if res["ok"]:
        try:
            res["leases"] = json.loads(res["stdout"] or "[]")
        except ValueError:
            res["ok"] = False
            res["stderr"] += "\nlease list --json did not return JSON"
    return res


def status() -> Dict[str, Any]:
    res = _run(["state", "--json"])
    if res["ok"]:
        try:
            res["state"] = json.loads(res["stdout"])
        except ValueError:
            res["state"] = None  # the text form stays in stdout
    return res


def fresh(ref: str, paths: Any) -> Dict[str, Any]:
    return _run(["fresh", _no_flag(ref, "ref"), *_paths(paths)])


def read(ref: str, path: str) -> Dict[str, Any]:
    res = _run(["read", _no_flag(ref, "ref"), _no_flag(path, "path")])
    body = res.pop("stdout")
    res["truncated"] = len(body.encode("utf-8")) > READ_MAX_BYTES
    res["content"] = body.encode("utf-8")[:READ_MAX_BYTES].decode("utf-8", "ignore")
    return res


def blob_commit(paths: Any, message: str, base: str, branch: str = "",
                advance: bool = False) -> Dict[str, Any]:
    names = _paths(paths)
    msg = (message or "").strip()
    if not msg:
        raise RefusedError("message is required")
    base = _no_flag(base, "base")
    branch = (branch or "").strip()
    if branch:
        _no_flag(branch, "branch")
    if advance and not branch:
        raise RefusedError("advance needs a branch — it fast-forwards refs/heads/<branch>")
    for n in names:
        full = _inside_repo(n)
        if full.is_dir():
            raise RefusedError(f"{n!r} is a directory — name the files, never a tree")
        if not full.exists():
            raise RefusedError(f"{n!r} does not exist on disk, so blob-commit would record "
                          "it as a DELETION. Deletions are CLI-only, on purpose")
    argv = ["blob-commit", f"--base={base}", f"--message={msg}"]
    if branch:
        argv.append(f"--branch={branch}")
    if advance:
        argv.append("--advance")
    return _run([*argv, "--", *names], timeout=TIMEOUT_SLOW)


_S = {"type": "string"}
_PATHS = {"type": "array", "items": _S, "minItems": 1}
_ACTOR = {"type": "string", "description": (
    "lease owner; default is derived exactly as the CLI derives it. Pass "
    "claude:<CLAUDE_CODE_SESSION_ID> so your shell-side commits match")}

TOOLS: Dict[str, Tuple[Callable[..., Dict[str, Any]], str, Dict[str, Any]]] = {
    "lease_acquire": (lease_acquire, "Lease files before editing them (all-or-nothing; "
                      "refused if another session holds one).",
                      {"type": "object", "required": ["paths"], "properties": {
                          "paths": _PATHS, "ttl_sec": {"type": "integer", "default": 300},
                          "reason": _S, "actor": _ACTOR}}),
    "lease_release": (lease_release, "Release your leases, by lease id or leased path.",
                      {"type": "object", "required": ["targets"], "properties": {
                          "targets": _PATHS, "actor": _ACTOR}}),
    "lease_list": (lease_list, "List every active lease (who holds which file).",
                   {"type": "object", "properties": {}}),
    "status": (status, "Where you are: branch, worktree, stack, open PRs, merge state.",
               {"type": "object", "properties": {}}),
    "fresh": (fresh, "Is my copy of each path BEHIND a ref? Run before editing a file "
              "peers also move.",
              {"type": "object", "required": ["ref", "paths"], "properties": {
                  "ref": _S, "paths": _PATHS}}),
    "read": (read, "Read a file at another ref (refuses rather than returning a silent "
             "empty read).",
             {"type": "object", "required": ["ref", "path"], "properties": {
                 "ref": _S, "path": _S}}),
    "blob_commit": (blob_commit, "Commit EXACTLY these existing files onto base via a "
                    "private index; the shared index and worktree are never touched. "
                    "With advance+branch, fast-forwards the branch (refused if a peer "
                    "moved it).",
                    {"type": "object", "required": ["paths", "message", "base"],
                     "properties": {"paths": _PATHS, "message": _S, "base": _S,
                                    "branch": _S,
                                    "advance": {"type": "boolean", "default": False}}}),
}


def call_tool(name: str, arguments: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """MCP ``tools/call`` result for one tool invocation."""
    entry = TOOLS.get(name)
    if entry is None:
        return _text({"ok": False, "error": f"unknown tool {name!r}"}, True)
    fn, _desc, schema = entry
    args = dict(arguments or {})
    unknown = set(args) - set(schema.get("properties", {}))
    if unknown:
        return _text({"ok": False, "refused": True,
                      "error": f"unknown argument(s): {sorted(unknown)}"}, True)
    try:
        res = fn(**args)
    except RefusedError as exc:
        return _text({"ok": False, "refused": True, "error": str(exc)}, True)
    except (TypeError, ValueError) as exc:
        return _text({"ok": False, "refused": True, "error": f"bad arguments: {exc}"}, True)
    return _text(res, not res.get("ok", False))


def _text(obj: Dict[str, Any], is_error: bool) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(obj, indent=2)}],
            "structuredContent": obj, "isError": is_error}


def handle(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One JSON-RPC message in, one response out (None for a notification)."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # notifications (initialized, cancelled) need no reply
    if method == "initialize":
        from awgit import __version__

        want = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        result: Dict[str, Any] = {
            "protocolVersion": want, "capabilities": {"tools": {}},
            "serverInfo": {"name": "awgit", "version": __version__},
            "instructions": ("Leases and safe commits for a shared git checkout. "
                             "lease_acquire before editing, fresh before editing a "
                             "file peers move, blob_commit to commit exactly your "
                             "files.")}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": s}
                            for n, (_f, d, s) in TOOLS.items()]}
    elif method == "tools/call":
        params = msg.get("params") or {}
        result = call_tool(str(params.get("name", "")), params.get("arguments"))
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=None, stdout=None) -> int:
    """Serve until stdin closes. Each line is one JSON-RPC message."""
    src = stdin if stdin is not None else sys.stdin.buffer
    out = stdout if stdout is not None else sys.stdout.buffer
    for raw in src:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply: Optional[Dict[str, Any]] = {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = handle(msg) if isinstance(msg, dict) else {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "invalid request"}}
        if reply is not None:
            out.write(json.dumps(reply).encode("utf-8") + b"\n")
            out.flush()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    try:
        return serve()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
