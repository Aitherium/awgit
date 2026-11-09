"""`awgit capture --range` skips its run when the host is short of commit.

The hourly trunk backfill started regardless of host memory and ran during a
97.5% commit spike on 2026-11-08 (this PC has rebooted at ~96%). The range
capture is idempotent and resumes, so a skip (exit 0) loses nothing.
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
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "commit.gpgsign", "false")
    for name, body in (("base.txt", "root\n"), ("a.py", "def a():\n    return 1\n")):
        (r / name).write_text(body, encoding="utf-8")
        git(r, "add", name)
        git(r, "-c", "user.email=t@example.invalid", "-c", "user.name=t",
            "commit", "-q", "--no-verify", "-m", f"touch {name}")
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "vcs"))
    monkeypatch.delenv("AWGIT_CAPTURE_MAX_COMMIT", raising=False)
    monkeypatch.chdir(r)
    return r


def ops(tmp_path: Path) -> int:
    return len(OpLog(data_root=tmp_path / "vcs").all_ops())


def test_skips_at_or_above_the_default_limit(repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_host_commit_pct", lambda: 91.2)
    assert cli.main(["capture", "--range", "HEAD"]) == 0
    out = capsys.readouterr().out
    assert "skipped: host commit 91.2% >= limit 85%" in out
    assert "next run resumes" in out
    assert ops(tmp_path) == 0
    monkeypatch.setattr(cli, "_host_commit_pct", lambda: 85.0)  # the limit itself skips
    assert cli.main(["capture", "--range", "HEAD"]) == 0
    assert "skipped" in capsys.readouterr().out


def test_runs_below_the_limit(repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_host_commit_pct", lambda: 60.0)
    assert cli.main(["capture", "--range", "HEAD"]) == 0
    assert "skipped" not in capsys.readouterr().out
    assert ops(tmp_path) == 1


def test_env_overrides_the_limit(repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_host_commit_pct", lambda: 91.2)
    monkeypatch.setenv("AWGIT_CAPTURE_MAX_COMMIT", "95")
    assert cli.main(["capture", "--range", "HEAD"]) == 0
    assert "skipped" not in capsys.readouterr().out
    assert ops(tmp_path) == 1
    monkeypatch.setenv("AWGIT_CAPTURE_MAX_COMMIT", "50%")
    monkeypatch.setattr(cli, "_host_commit_pct", lambda: 60.0)
    assert cli.main(["capture", "--range", "HEAD"]) == 0
    assert "limit 50%" in capsys.readouterr().out


def test_an_unmeasurable_host_does_not_skip(repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_host_commit_pct", lambda: None)
    assert cli.main(["capture", "--range", "HEAD"]) == 0
    assert ops(tmp_path) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="GlobalMemoryStatusEx is Windows-only")
def test_real_reading_is_a_percentage():
    pct = cli._host_commit_pct()
    assert pct is not None and 0.0 < pct <= 100.0
