"""`awgit mcp`: every tool works against a real repository, and every refusal refuses.

The refusals are the point of the server. Each one is asserted to leave the
repository exactly as it was: a refusal that ran half the command first is not a
refusal.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from awgit import mcp_server as srv

PKG = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "core.autocrlf", "false")
    (r / "a.txt").write_bytes("one\n".encode())
    (r / "sub").mkdir()
    (r / "sub" / "b.txt").write_bytes("two\n".encode())
    _git(r, "add", "a.txt", "sub/b.txt")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "branch", "feat/x")
    monkeypatch.chdir(r)
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "vcsdata"))
    monkeypatch.setenv("AITHER_ACTOR", "claude:test-me")
    # The child `python -m awgit` must run THIS tree's awgit, not an installed one.
    monkeypatch.setenv("PYTHONPATH", str(PKG) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return r


def _call(name: str, **args):
    res = srv.call_tool(name, args)
    return res["isError"], res["structuredContent"]


# ── protocol ────────────────────────────────────────────────────────────────

def test_initialize_and_tools_list():
    init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["serverInfo"]["name"] == "awgit"
    assert init["result"]["capabilities"] == {"tools": {}}
    tools = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in tools["result"]["tools"]}
    assert names == {"lease_acquire", "lease_release", "lease_list", "status", "fresh",
                     "read", "blob_commit"}


def test_notification_gets_no_reply_and_unknown_method_errors():
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    err = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    assert err["error"]["code"] == -32601


def test_unknown_tool_and_unknown_argument_are_errors():
    assert srv.call_tool("nope", {})["isError"]
    bad, out = _call("lease_list", sneaky=1)
    assert bad and out["refused"]


def test_cli_subcommand_serves_stdio(repo):
    """`python -m awgit mcp` speaks the protocol end to end over real pipes."""
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "read", "arguments": {"ref": "HEAD", "path": "a.txt"}}}]
    p = subprocess.run([sys.executable, "-m", "awgit", "mcp"],
                       input="".join(json.dumps(m) + "\n" for m in msgs).encode(),
                       capture_output=True, timeout=180, cwd=str(repo))
    assert p.returncode == 0, p.stderr
    replies = [json.loads(line) for line in p.stdout.decode().splitlines()]
    assert [r["id"] for r in replies] == [1, 2], "a notification must not be answered"
    body = replies[1]["result"]["structuredContent"]
    assert body["ok"] and body["content"] == "one\n"


# ── leases ──────────────────────────────────────────────────────────────────

def test_lease_acquire_list_conflict_release(repo):
    bad, out = _call("lease_acquire", paths=["a.txt"], reason="edit")
    assert not bad, out
    bad, out = _call("lease_list")
    assert not bad
    assert [(lz["target"], lz["actor"]) for lz in out["leases"]] == [("a.txt", "claude:test-me")]
    bad, out = _call("lease_acquire", paths=["a.txt"], actor="claude:someone-else")
    assert bad and out["exit_code"] == 1, "a peer's lease must refuse the second acquire"
    bad, out = _call("lease_release", targets=["a.txt"])
    assert not bad, out
    assert _call("lease_list")[1]["leases"] == []


def test_lease_refusals(repo):
    for kwargs in ({"paths": []}, {"paths": ["../outside.txt"]}, {"paths": ["--staged"]},
                   {"paths": ["a.txt"], "actor": "--adopt"}):
        bad, out = _call("lease_acquire", **kwargs)
        assert bad and out["refused"], kwargs
    assert _call("lease_list")[1]["leases"] == [], "a refusal acquired something"
    bad, out = _call("lease_release", targets=[])
    assert bad and out["refused"]


# ── read / fresh / status ───────────────────────────────────────────────────

def test_read_fresh_status(repo):
    bad, out = _call("read", ref="HEAD", path="sub/b.txt")
    assert not bad and out["content"] == "two\n" and out["truncated"] is False
    bad, out = _call("read", ref="HEAD", path="missing.txt")
    assert bad, "an absent path must not read as an empty file"
    bad, out = _call("read", ref="--output=x", path="a.txt")
    assert bad and out["refused"]
    bad, out = _call("fresh", ref="HEAD", paths=["a.txt"])
    assert not bad, out
    bad, out = _call("fresh", ref="HEAD", paths=[])
    assert bad and out["refused"]
    bad, out = _call("status")
    assert out["exit_code"] is not None


# ── blob_commit ─────────────────────────────────────────────────────────────

def test_blob_commit_commits_exactly_the_named_file_and_advances(repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_bytes("one\nmine\n".encode())
    (repo / "sub" / "b.txt").write_bytes("a peer's edit\n".encode())
    _git(repo, "add", "sub/b.txt")  # a peer's STAGED work must not ride along
    bad, out = _call("blob_commit", paths=["a.txt"], message="mine only", base=base,
                     branch="feat/x", advance=True)
    assert not bad, out
    tip = _git(repo, "rev-parse", "feat/x")
    assert tip != base
    assert _git(repo, "diff", "--name-only", base, tip) == "a.txt"
    assert _git(repo, "show", f"{tip}:a.txt") == "one\nmine"
    # The peer's staged blob is still staged, byte for byte (blob-commit only
    # re-points the index entry of a path it committed).
    assert "sub/b.txt" in _git(repo, "diff", "--cached", "--name-only").split()
    assert _git(repo, "show", ":sub/b.txt") == "a peer's edit"
    assert _git(repo, "rev-parse", "HEAD") == base, "HEAD moved"


@pytest.mark.parametrize("kwargs,why", [
    ({"paths": []}, "pathspec-less"),
    ({"paths": ["gone.txt"]}, "missing path would become a deletion"),
    ({"paths": ["sub"]}, "a directory is a tree, not a file list"),
    ({"paths": ["../x.txt"]}, "outside the repo"),
    ({"paths": ["--untrack=a.txt"]}, "a flag smuggled as a path"),
    ({"paths": ["a.txt"], "message": ""}, "empty message"),
    ({"paths": ["a.txt"], "base": "--upload-pack=evil"}, "a flag smuggled as a ref"),
    ({"paths": ["a.txt"], "advance": True, "branch": ""}, "advance without a branch"),
])
def test_blob_commit_refusals_change_nothing(repo, kwargs, why):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_bytes("changed\n".encode())
    args = {"message": "m", "base": base, "branch": "feat/x", **kwargs}
    bad, out = _call("blob_commit", **args)
    assert bad and out.get("refused"), why
    assert _git(repo, "rev-parse", "feat/x") == base, f"{why}: branch moved"
    assert _git(repo, "diff", "--cached", "--name-only") == "", f"{why}: index touched"


def test_blob_commit_refused_when_a_peer_moved_the_branch(repo):
    """The CLI's own compare-and-swap refusal reaches the tool result as an error."""
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_bytes("peer\n".encode())
    _call("blob_commit", paths=["a.txt"], message="peer", base=base, branch="feat/x",
          advance=True)
    peer_tip = _git(repo, "rev-parse", "feat/x")
    (repo / "a.txt").write_bytes("me, stale base\n".encode())
    bad, out = _call("blob_commit", paths=["a.txt"], message="stale", base=base,
                     branch="feat/x", advance=True)
    assert bad and not out.get("refused"), "refusal must come from the CLI, not this layer"
    assert _git(repo, "rev-parse", "feat/x") == peer_tip
