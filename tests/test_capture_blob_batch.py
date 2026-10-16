"""git_blob reads through one `git cat-file --batch`, byte-identical to `git show`.

Measured 2026-10-15: a spawn per blob made one 44-file capture take 22.9 s; the
batch reader took 4.1 s for the same 167 node changes (#13045).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit import capture  # noqa: E402


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.py").write_bytes(b"def a():\r\n    return '\xc3\xa9'\r\n")
    (r / "empty.py").write_bytes(b"")
    (r / "sub").mkdir()
    (r / "sub" / "b.py").write_bytes(b"x = 1\n" * 5000)
    git(r, "add", "-A")
    git(r, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--no-verify", "-m", "c")
    yield r
    capture._BlobBatch.close_all()


def test_batch_matches_git_show_byte_for_byte(repo: Path):
    sha = git(repo, "rev-parse", "HEAD").strip()
    for rel in ("a.py", "empty.py", "sub/b.py"):
        want = subprocess.run(["git", "show", f"{sha}:{rel}"], cwd=str(repo),
                              capture_output=True, check=True).stdout
        assert capture.git_blob(repo, sha, rel) == want, rel


def test_missing_path_and_tree_return_none_and_stream_stays_in_sync(repo: Path):
    sha = git(repo, "rev-parse", "HEAD").strip()
    assert capture.git_blob(repo, sha, "nope.py") is None
    assert capture.git_blob(repo, sha, "sub") is None  # a tree, not a blob
    assert capture.git_blob(repo, sha, "a.py").startswith(b"def a():")


def test_dead_batch_process_is_replaced(repo: Path):
    sha = git(repo, "rev-parse", "HEAD").strip()
    assert capture.git_blob(repo, sha, "a.py")
    for b in capture._BlobBatch._by_repo.values():
        b.close()
        b.proc.wait()
    assert capture.git_blob(repo, sha, "a.py").startswith(b"def a():")


def test_oversized_blob_skips_node_diff_but_small_files_still_count(repo: Path, tmp_path: Path,
                                                                    monkeypatch):
    from awgit.oplog import OpLog

    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "vcs"))
    monkeypatch.setattr(capture, "_MAX_PARSE_BYTES", 1024)
    (repo / "big.py").write_text("def big():\n    return 1\n" + "x = 1\n" * 2000,
                                 encoding="utf-8")
    (repo / "small.py").write_text("def small():\n    return 2\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--no-verify",
        "-m", "big and small")
    sha = git(repo, "rev-parse", "HEAD").strip()
    op = capture.capture_ops(sha, actor="t", repo_path=str(repo), data_root=tmp_path / "vcs")
    assert op is not None
    assert {c.path for c in op.node_changes} == {"small.py"}
    assert "big.py" in op.file_paths
    assert OpLog(data_root=tmp_path / "vcs").has_commit(sha)
