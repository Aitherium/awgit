"""blob-commit reaches the op-log, and `awgit evidence` says how much does.

blob-commit builds its commit with ``commit-tree``, and git fires no hooks for
that, so the post-commit ``vcs-capture`` fragment never saw any of them. The
op-log held 4,079 ops against ~5,700 commits a month (#13045). These pin the
fix: a blob-commit is captured exactly once, a re-capture adds nothing, a
capture failure never fails the commit, and evidence prints the coverage line.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit import cli  # noqa: E402
from awgit.capture import capture_ops  # noqa: E402
from awgit.oplog import OpLog  # noqa: E402
from awgit.scratch import _blob_commit  # noqa: E402


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@e.com")
    git(r, "config", "user.name", "T")
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "vcs"))
    monkeypatch.setenv("VCS_LEASES_ENFORCE", "0")
    monkeypatch.setenv("AITHER_ACTOR", "agent-under-test")
    monkeypatch.chdir(r)
    (r / "m.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    git(r, "add", "m.py")
    git(r, "commit", "-q", "--no-verify", "-m", "seed")
    return r


def _edit_and_blob_commit(repo: Path, body: str = "2") -> str:
    (repo / "m.py").write_text(
        f"def alpha():\n    return {body}\n\n\ndef beta():\n    return 3\n",
        encoding="utf-8")
    rc, sha = _blob_commit("HEAD", "main", "edit alpha, add beta", ["m.py"],
                           repo=repo, advance=True)
    assert rc == 0 and sha, (rc, sha)
    return sha


def test_blob_commit_produces_exactly_one_op_for_its_sha(repo: Path, tmp_path):
    sha = _edit_and_blob_commit(repo)
    ops = OpLog(data_root=tmp_path / "vcs").ops_for_commit(sha)
    assert len(ops) == 1, ops
    assert ops[0].actor == "agent-under-test"
    assert ops[0].git_sha == sha


def test_recapture_of_the_same_sha_is_a_noop(repo: Path, tmp_path):
    sha = _edit_and_blob_commit(repo)
    # The hook path and the CLI both reaching the same sha must not double-log.
    capture_ops(sha, repo_path=str(repo))
    assert cli.main(["capture", "--sha", sha]) == 0
    log = OpLog(data_root=tmp_path / "vcs")
    assert len(log.ops_for_commit(sha)) == 1
    assert len(log.all_ops()) == 1


def test_capture_failure_warns_and_never_fails_the_commit(repo: Path, capsys,
                                                          monkeypatch):
    import awgit.capture as cap

    def boom(*_a, **_k):
        raise RuntimeError("store unwritable")

    monkeypatch.setattr(cap, "capture_ops", boom)
    sha = _edit_and_blob_commit(repo)
    out = capsys.readouterr().out
    assert "WARNING op-log capture" in out and "store unwritable" in out
    assert git(repo, "rev-parse", "main").strip() == sha


def test_evidence_prints_the_coverage_line(repo: Path, capsys):
    _edit_and_blob_commit(repo)
    assert cli.main(["evidence"]) == 0
    out = capsys.readouterr().out
    # 2 commits on main (the seed root commit has no parent -> no op), 1 op.
    assert "op-log coverage     50.0% (1/2 commits" in out, out


def test_evidence_coverage_is_not_measured_outside_a_repo(tmp_path, capsys,
                                                          monkeypatch):
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "vcs"))
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    monkeypatch.chdir(bare)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert cli.main(["evidence"]) == 0
    assert "op-log coverage     NOT MEASURED" in capsys.readouterr().out


def test_evidence_json_carries_coverage(repo: Path, capsys):
    import json

    _edit_and_blob_commit(repo)
    capsys.readouterr()  # drop blob-commit's own output
    assert cli.main(["evidence", "--json"]) == 0
    ev = json.loads(capsys.readouterr().out)
    assert ev["coverage"]["commits"] == 2 and ev["coverage"]["captured"] == 1
    assert os.environ["VCS_DATA_ROOT"]  # sanity: the store was isolated
