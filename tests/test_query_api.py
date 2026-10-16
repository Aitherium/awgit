"""Read-only repository queries: ask the repository, don't check it out.

Each tool is exercised against a real temporary git repository, and each of
the refusals that make it safe to expose over MCP is pinned: a path that
escapes the repository, a rev that would become a git option, and a caller
trying to choose which repository gets read.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from awgit import mcp
from awgit import query as q

BASE = (
    "def alpha(x):\n"
    "    return x + 1\n"
    "\n"
    "\n"
    "def beta(y):\n"
    "    return y * 2\n"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(repo: Path, rel: str, body: str, msg: str) -> str:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(body, encoding="utf-8", newline="\n")
    _git(repo, "add", "--", rel)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """One repository for the module: building it is ~20 git spawns."""
    r = tmp_path_factory.mktemp("query") / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "Tester")
    _git(r, "config", "core.autocrlf", "false")
    shas = {"base": _commit(r, "pkg/mod.py", BASE, "base")}
    shas["notes"] = _commit(r, "notes.txt", "one\ntwo\n", "notes")
    # Two branches off `notes`: a edits alpha, b edits beta (disjoint nodes).
    _git(r, "switch", "-q", "-c", "a")
    shas["a"] = _commit(r, "pkg/mod.py", BASE.replace("x + 1", "x + 10"), "a: alpha")
    _git(r, "switch", "-q", "-c", "b", shas["notes"])
    shas["b"] = _commit(r, "pkg/mod.py", BASE.replace("y * 2", "y * 20"), "b: beta")
    # c edits alpha too, differently from a -- a real node-level collision.
    _git(r, "switch", "-q", "-c", "c", shas["notes"])
    shas["c"] = _commit(r, "pkg/mod.py",
                        BASE.replace("    return x + 1\n", "    y = x\n    return y - 7\n"),
                        "c: alpha differently")
    _git(r, "switch", "-q", "main")
    return r, shas


@pytest.fixture
def repo(built, tmp_path, monkeypatch):
    r, shas = built
    monkeypatch.setenv("VCS_REPO_ROOT", str(r))
    data = tmp_path / "vcs-data"
    monkeypatch.setenv("VCS_DATA_ROOT", str(data))
    return r, shas, data


def test_tools_are_on_the_mcp_surface():
    for name in ("vcs_blame", "vcs_path_log", "vcs_merge_base", "vcs_file_at",
                 "vcs_diff", "vcs_merge_preview"):
        assert name in mcp.TOOLS, f"{name} is not in awgit.mcp.TOOLS"


def test_blame_names_the_commit_per_line(repo):
    r, shas, _ = repo
    out = q.vcs_blame("pkg/mod.py", rev="a")
    assert "error" not in out, out
    assert out["count"] == 6 and out["truncated"] is False
    by_line = {ln["line"]: ln for ln in out["lines"]}
    assert by_line[2]["sha"] == shas["a"] and by_line[2]["content"] == "    return x + 10"
    assert by_line[1]["sha"] == shas["base"] and by_line[1]["author"] == "Tester"
    ranged = q.vcs_blame("pkg/mod.py", rev="a", start_line=5, end_line=6)
    assert [ln["line"] for ln in ranged["lines"]] == [5, 6]
    assert "error" in q.vcs_blame("pkg/mod.py", start_line=4, end_line=2)


def test_path_log_lists_commits_touching_the_path(repo):
    r, shas, _ = repo
    out = q.vcs_path_log("pkg/mod.py", rev="a")
    assert [c["sha"] for c in out["commits"]] == [shas["a"], shas["base"]]
    assert out["truncated"] is False
    one = q.vcs_path_log("pkg/mod.py", rev="a", limit=1)
    assert one["count"] == 1 and one["truncated"] is True
    assert q.vcs_path_log("notes.txt", rev="a")["commits"][0]["subject"] == "notes"


def test_merge_base(repo):
    r, shas, _ = repo
    out = q.vcs_merge_base("a", "b")
    assert out["merge_base"] == shas["notes"]
    assert out["a"] == shas["a"] and out["b"] == shas["b"]
    assert q.vcs_merge_base("main", "a")["a_is_ancestor"] is True


def test_file_at_reads_history_without_a_checkout(repo):
    r, shas, _ = repo
    out = q.vcs_file_at("pkg/mod.py", rev="b")
    assert "y * 20" in out["content"] and out["truncated"] is False
    assert (r / "pkg/mod.py").read_text(encoding="utf-8") == BASE, "worktree was touched"
    capped = q.vcs_file_at("pkg/mod.py", rev="b", max_bytes=10)
    assert capped["truncated"] is True and len(capped["content"]) == 10
    assert capped["size"] == len(BASE.replace("y * 2", "y * 20"))
    assert "error" in q.vcs_file_at("nope.py")
    assert "not a file" in q.vcs_file_at("pkg")["error"]


def test_diff_unified_numstat_and_path_filter(repo):
    r, shas, _ = repo
    out = q.vcs_diff("main", "a")
    assert "+    return x + 10" in out["diff"] and out["truncated"] is False
    stat = q.vcs_diff(shas["notes"], "a", stat_only=True)
    assert stat["files"] == [{"path": "pkg/mod.py", "binary": False,
                              "added": 1, "deleted": 1}]
    assert q.vcs_diff(shas["base"], "a", paths=["pkg/mod.py"])["diff"].count("diff --git") == 1
    assert q.vcs_diff(shas["base"], "a", paths=["notes.txt"], stat_only=True)["count"] == 1
    capped = q.vcs_diff(shas["base"], "a", max_bytes=20)
    assert capped["truncated"] is True and len(capped["diff"]) == 20


def test_merge_preview_disjoint_is_clean(repo):
    out = q.vcs_merge_preview("a", "b")
    assert "error" not in out, out
    assert out["status"] == "clean" and out["conflicts"] == []
    assert out["files"] == {"a": 1, "b": 1}


def test_merge_preview_collision_is_reported_but_never_filed(repo):
    r, shas, data = repo
    out = q.vcs_merge_preview("a", "c")
    assert out["status"] == "conflict", out
    assert out["conflicts"] and out["conflicts"][0]["symbol"].endswith("alpha")
    # A preview must not escalate into the real conflict inbox.
    assert mcp.vcs_merge_conflicts(data_root=str(data))["count"] == 0


@pytest.mark.parametrize("bad", [
    "../outside.py", "pkg/../../outside.py", "/etc/passwd", "C:/Windows/win.ini",
    "c:\\Windows\\win.ini", "--output=/tmp/pwn", ":(top)pkg/mod.py", "", "a\x00b",
])
def test_paths_that_escape_or_inject_are_refused(repo, bad):
    _, shas, _ = repo
    for out in (q.vcs_file_at(bad), q.vcs_blame(bad), q.vcs_path_log(bad),
                q.vcs_diff(shas["base"], "a", paths=[bad])):
        assert "error" in out, f"{bad!r} was not refused: {out}"


@pytest.mark.parametrize("bad", [
    "--output=/tmp/pwn", "-p", "base..a", "HEAD:pkg/mod.py", "no-such-ref", "",
    "a b", "deadbeef" * 5,
])
def test_revs_that_inject_or_do_not_resolve_are_refused(repo, bad):
    for out in (q.vcs_file_at("pkg/mod.py", rev=bad), q.vcs_merge_base(bad, "a"),
                q.vcs_diff("a", bad), q.vcs_merge_preview(bad, "a")):
        assert "error" in out, f"rev {bad!r} was not refused: {out}"


def test_relative_revs_still_resolve(repo):
    r, shas, _ = repo
    assert q.vcs_file_at("pkg/mod.py", rev="a~1")["rev"] == shas["notes"]
    assert q.vcs_merge_base("a^", "b")["a"] == shas["notes"]


def test_caller_cannot_choose_the_repository(repo, tmp_path):
    r, shas, _ = repo
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _git(other, "config", "user.email", "o@example.com")
    _git(other, "config", "user.name", "Other")
    _commit(other, "pkg/mod.py", "SECRET = 1\n", "other")
    for kw in ({"repo_path": str(other)}, {"repo": str(other)}, {"cwd": str(other)}):
        out = q.vcs_file_at("pkg/mod.py", rev="main", **kw)
        assert out["content"] == BASE, f"{kw} redirected the read"


def test_no_repository_is_a_structured_error(tmp_path, monkeypatch):
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    monkeypatch.setenv("VCS_REPO_ROOT", str(empty))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    out = q.vcs_merge_base("HEAD", "HEAD")
    assert "no git repository" in out["error"]


# ── byte fidelity, non-ASCII paths, byte caps, timeouts, bare repositories ──

COLLIDE = "def f():\n    return 1\n"


@pytest.fixture(scope="module")
def odd(tmp_path_factory):
    """A repository of the shapes a Windows monorepo actually has: CRLF bodies,
    non-ASCII paths, multi-byte text, a non-UTF-8 blob and a rename."""
    r = tmp_path_factory.mktemp("odd") / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "Tester")
    _git(r, "config", "core.autocrlf", "false")
    (r / "crlf.ps1").write_bytes(b"a\r\nb\r\n")
    (r / "wide.txt").write_bytes(("é" * 300).encode("utf-8"))
    (r / "latin.txt").write_bytes(b"caf\xe9\n")
    (r / "old_name.txt").write_bytes(b"x\n" * 20)
    for name in ("café.py", "plain.py"):
        (r / name).write_text(COLLIDE, encoding="utf-8", newline="\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    shas = {"base": _git(r, "rev-parse", "HEAD")}
    for branch, value in (("x", "2"), ("y", "3")):
        _git(r, "switch", "-q", "-c", branch, shas["base"])
        (r / "café.py").write_text(COLLIDE.replace("1", value), encoding="utf-8",
                                       newline="\n")
        (r / "crlf.ps1").write_bytes(b"a\r\nb\r\nc" + value.encode() + b"\r\n")
        _git(r, "mv", "old_name.txt", f"new_{branch}.txt")
        _git(r, "add", "-A")
        _git(r, "commit", "-q", "-m", f"{branch}: edit f")
        shas[branch] = _git(r, "rev-parse", "HEAD")
    _git(r, "switch", "-q", "main")
    return r, shas


@pytest.fixture
def odd_repo(odd, tmp_path, monkeypatch):
    r, shas = odd
    monkeypatch.setenv("VCS_REPO_ROOT", str(r))
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "vcs-data"))
    return r, shas


def test_file_at_is_byte_exact_on_crlf(odd_repo):
    out = q.vcs_file_at("crlf.ps1")
    assert out["content"] == "a\r\nb\r\n", repr(out["content"])
    assert out["lossy"] is False and out["truncated"] is False


def test_blame_and_diff_keep_carriage_returns(odd_repo):
    _, shas = odd_repo
    lines = q.vcs_blame("crlf.ps1", rev="x")["lines"]
    assert [ln["content"] for ln in lines] == ["a\r", "b\r", "c2\r"]
    assert "+c2\r\n" in q.vcs_diff(shas["base"], "x", paths=["crlf.ps1"])["diff"]


def test_non_ascii_paths_are_never_quoted(odd_repo):
    _, shas = odd_repo
    stat = q.vcs_diff(shas["base"], "x", stat_only=True)
    assert "café.py" in [f["path"] for f in stat["files"]], stat
    assert "diff --git a/café.py" in q.vcs_diff(shas["base"], "x")["diff"]


def test_numstat_reports_renames_with_their_source(odd_repo):
    _, shas = odd_repo
    files = q.vcs_diff(shas["base"], "x", stat_only=True)["files"]
    renamed = [f for f in files if f["path"] == "new_x.txt"]
    assert renamed and renamed[0]["from"] == "old_name.txt", files


def test_merge_preview_sees_a_collision_in_a_non_ascii_file(odd_repo):
    out = q.vcs_merge_preview("x", "y")
    assert out["status"] == "conflict", out
    assert any("café.py" in str(c) for c in out["conflicts"]), out["conflicts"]


def test_max_bytes_counts_bytes_not_characters(odd_repo):
    out = q.vcs_file_at("wide.txt", max_bytes=101)
    assert out["truncated"] is True
    assert len(out["content"].encode("utf-8")) <= 101
    assert out["content"] == "é" * 50, "a cut mid-character must drop it, not invent one"
    diff = q.vcs_diff("x", "y", max_bytes=57)
    assert len(diff["diff"].encode("utf-8")) <= 57 and diff["truncated"] is True


def test_non_utf8_blob_is_flagged_lossy(odd_repo):
    out = q.vcs_file_at("latin.txt")
    assert out["lossy"] is True and "�" in out["content"]


def test_capped_read_is_killed_by_the_watchdog(odd_repo, monkeypatch):
    monkeypatch.setattr(q, "GIT_TIMEOUT_SEC", 1)
    monkeypatch.setattr(q, "_git_argv", lambda repo, args: [
        sys.executable, "-c", "import time; time.sleep(30)"])
    t0 = time.monotonic()
    with pytest.raises(q.QueryError, match="timed out"):
        q._git_capped_bytes(odd_repo[0], ["cat-file"], 100)
    assert time.monotonic() - t0 < 15, "read() blocked past the timeout"


def test_a_bare_git_directory_is_a_valid_repository(odd, tmp_path, monkeypatch):
    r, shas = odd
    bare = tmp_path / "mirror.git"
    _git(tmp_path, "clone", "-q", "--bare", str(r), str(bare))
    monkeypatch.setenv("VCS_REPO_ROOT", str(bare))
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "vcs-data"))
    out = q.vcs_file_at("crlf.ps1", rev="x")
    assert "error" not in out, out
    assert out["content"] == "a\r\nb\r\nc2\r\n" and out["rev"] == shas["x"]
    assert "error" in q.vcs_file_at("../crlf.ps1", rev="x")
    assert q.vcs_merge_base("x", "y")["merge_base"] == shas["base"]
