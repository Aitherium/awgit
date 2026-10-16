"""`awgit capture --range` backfills the trunk, once.

PRs land on develop as squash merges: new shas no hook ever saw, so measured
2026-10-15 op-log coverage of develop's 30-day history was 0 of 6,419 commits
while the op-log held 4,159 ops for branch shas (#13045). These pin the backfill:
every source-changing commit gets one op attributed to its author, a docs-only
commit records nothing, and a second run adds nothing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit import cli  # noqa: E402
from awgit.oplog import OpLog  # noqa: E402


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def commit(repo: Path, name: str, body: str, email: str) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "-c", f"user.email={email}", "-c", "user.name=t",
        "commit", "-q", "--no-verify", "-m", f"touch {name}")
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "commit.gpgsign", "false")
    commit(r, "base.txt", "root commit: capture has no parent to diff\n", "root@example.invalid")
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "vcs"))
    monkeypatch.chdir(r)
    return r


def test_range_backfills_once_with_author_actor(repo: Path, tmp_path: Path, capsys):
    a = commit(repo, "a.py", "def a():\n    return 1\n", "alice@example.invalid")
    commit(repo, "README.md", "docs only\n", "bob@example.invalid")
    b = commit(repo, "b.py", "def b():\n    return 2\n", "bob@example.invalid")

    assert cli.main(["capture", "--range", "HEAD"]) == 0
    first = capsys.readouterr().out
    assert "4 commits, 2 recorded, 2 with no semantic changes, 0 failed" in first

    log = OpLog(data_root=tmp_path / "vcs")
    assert {o.git_sha for o in log.all_ops()} == {a, b}
    assert log.ops_for_commit(a)[0].actor == "git-author:alice@example.invalid"

    assert cli.main(["capture", "--range", "HEAD"]) == 0
    assert "0 recorded" in capsys.readouterr().out
    assert len(OpLog(data_root=tmp_path / "vcs").all_ops()) == 2


def test_capture_without_sha_or_range_is_refused(repo: Path, capsys):
    assert cli.main(["capture"]) == 2
    assert "--sha SHA or --range REV" in capsys.readouterr().err


def test_bad_range_fails_loud(repo: Path, capsys):
    commit(repo, "a.py", "def a():\n    return 1\n", "alice@example.invalid")
    assert cli.main(["capture", "--range", "no-such-ref"]) == 2
    assert "failed" in capsys.readouterr().err


def test_busy_oplog_lock_is_retried_not_fatal(repo: Path, tmp_path: Path, monkeypatch, capsys):
    """A peer's post-commit capture holds the op-log lock; Windows raises EDEADLK.
    Measured 2026-10-15: one such OSError killed a 30-day backfill after 68 ops."""
    commit(repo, "a.py", "def a():\n    return 1\n", "alice@example.invalid")
    real = cli.capture_ops
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(36, "Resource deadlock avoided")
        return real(*a, **kw)

    monkeypatch.setattr(cli, "capture_ops", flaky)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["capture", "--range", "HEAD"]) == 0
    assert "1 recorded" in capsys.readouterr().out
    assert calls["n"] >= 2


def test_persistently_busy_lock_counts_as_failed_and_continues(repo: Path, monkeypatch, capsys):
    commit(repo, "a.py", "def a():\n    return 1\n", "alice@example.invalid")

    def busy(*a, **kw):
        raise OSError(36, "Resource deadlock avoided")

    monkeypatch.setattr(cli, "capture_ops", busy)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["capture", "--range", "HEAD"]) == 1
    out = capsys.readouterr()
    assert "2 failed" in out.out and "Resource deadlock" in out.err


def test_evidence_coverage_measures_the_named_ref(repo: Path, tmp_path: Path, capsys):
    from awgit.evidence import coverage

    git(repo, "switch", "-q", "-c", "side")
    commit(repo, "a.py", "def a():\n    return 1\n", "alice@example.invalid")
    assert cli.main(["capture", "--range", "side"]) == 0
    git(repo, "switch", "-q", "main")
    on_main = coverage(repo=repo, data_root=tmp_path / "vcs", ref="HEAD")
    on_side = coverage(repo=repo, data_root=tmp_path / "vcs", ref="side")
    assert on_main["captured"] == 0
    assert on_side["captured"] == 1 and on_side["ref"] == "side"
    assert coverage(repo=repo, ref="--all")["error"].startswith("refusing ref")


def test_backfilled_ops_do_not_count_against_lease_adoption(repo: Path, tmp_path: Path):
    from awgit.evidence import BACKFILL_ACTOR_PREFIX, gather

    commit(repo, "a.py", "def a():\n    return 1\n", "alice@example.invalid")
    assert cli.main(["capture", "--range", "HEAD"]) == 0
    ev = gather(data_root=tmp_path / "vcs")
    assert ev["ops"] == 1 and ev["backfill_ops"] == 1
    assert all(a.startswith(BACKFILL_ACTOR_PREFIX) for a in ev["actors"])
    assert ev["lease_adoption_pct"] == 0.0  # 0 leaseable ops, not 0/1
