"""Every documented command, actually executed.

These nine were documented and never run by anything. That combination is not
theoretical: `restack` was in exactly this state and silently dropped the top of
a stack while printing "HEAD is up to date". Nothing in the suite noticed,
because nothing in the suite ran it.

So these are deliberately shallow — they invoke each command through the real
CLI and assert the exit code and a property of the output. A shallow test that
RUNS the code catches "it does not work at all", which is the failure mode a
documented-but-unexercised command actually has. Depth is for the commands with
their own suites.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit import cli  # noqa: E402
from awgit.bridge import install_hooks  # noqa: E402


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env["VCS_LEASES_ENFORCE"] = "0"
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=env)
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
    install_hooks(str(r))
    monkeypatch.chdir(r)
    (r / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(r, "add", "seed.txt")
    git(r, "commit", "-q", "-m", "seed")
    (r / "m.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    git(r, "add", "m.py")
    git(r, "commit", "-q", "-m", "add alpha")
    return r


def run(*argv: str) -> int:
    return cli.main(list(argv))


# ── change-id ────────────────────────────────────────────────────────────

def test_change_id_show_and_find(repo: Path, capsys):
    assert run("change-id", "show", "--json") == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["change_id"].startswith("I"), shown

    assert run("change-id", "find", shown["change_id"], "--json") == 0
    found = json.loads(capsys.readouterr().out)
    assert git(repo, "rev-parse", "HEAD").strip() in found["commits"]


def test_change_id_show_fails_loudly_on_an_unstamped_commit(repo: Path, capsys):
    from awgit.bridge import uninstall_hooks

    uninstall_hooks(str(repo))
    (repo / "n.py").write_text("def n():\n    return 1\n", encoding="utf-8")
    git(repo, "add", "n.py")
    git(repo, "commit", "-q", "-m", "unstamped")
    assert run("change-id", "show") == 1, "a missing id must not exit 0"


# ── commands / dedupe / lease-check / stage-mine ─────────────────────────

def test_commands_json_is_parseable_and_complete(capsys):
    assert run("commands", "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["contract"] and doc["commands"]
    names = {c["name"] for c in doc["commands"]}
    for expected in ("push", "stack", "absorb", "review", "prove"):
        assert expected in names


def test_dedupe_reports_the_body_store(repo: Path, capsys):
    assert run("dedupe") == 0
    assert "blobs" in capsys.readouterr().out


def test_lease_check_passes_when_nothing_is_staged(repo: Path):
    assert run("lease-check") == 0


def test_stage_mine_self_test_proves_its_own_merge(repo: Path):
    """The command ships a --self-test; running it is the point."""
    assert run("stage-mine", "--self-test") == 0


# ── merge-preview / merge-conflicts / resolve-conflict ───────────────────

def test_merge_preview_reports_a_status(repo: Path, capsys):
    head = git(repo, "rev-parse", "HEAD").strip()
    parent = git(repo, "rev-parse", "HEAD~1").strip()
    assert run("merge-preview", parent, head, "--json") in (0, 1)
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"], payload


def test_merge_conflicts_lists_nothing_on_a_clean_store(repo: Path, capsys):
    assert run("merge-conflicts", "--json") == 0
    assert json.loads(capsys.readouterr().out)["count"] == 0


def test_resolve_conflict_refuses_an_unknown_id_rather_than_pretending(
        repo: Path, tmp_path: Path):
    body = tmp_path / "resolved.txt"
    body.write_text("def alpha():\n    return 2\n", encoding="utf-8")
    assert run("resolve-conflict", "does-not-exist", "--body", str(body)) == 1


def test_resolve_conflict_requires_a_body(repo: Path):
    assert run("resolve-conflict", "anything") == 2


# ── queue ────────────────────────────────────────────────────────────────

def test_queue_enqueue_uses_githubs_own_merge_queue(repo: Path, monkeypatch):
    """awgit must not reimplement a queue. Assert the shape of the call."""
    seen = {}

    def fake(argv, *a, **k):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", fake)
    assert run("queue", "enqueue", "42") == 0
    assert seen["argv"][:3] == ["gh", "pr", "merge"]
    assert "--auto" in seen["argv"], "must enqueue, not merge immediately"
    assert "42" in seen["argv"]


def test_queue_status_asks_github(repo: Path, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, *a, **k: (
        seen.update(argv=argv) or subprocess.CompletedProcess(argv, 0, "[]", "")))
    assert run("queue", "status") == 0
    assert seen["argv"][:3] == ["gh", "pr", "list"]
