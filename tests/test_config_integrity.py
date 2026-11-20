"""awgit integrity: a zeroed .git/config comes back byte for byte, under git's lock.

2026-11-13 and 2026-11-14 the deploy tree's .git/config was found all NUL bytes at its
old size after the host crashed (git renames config.lock into place and never fsyncs
it). These pin what the guard may and may not do about that.
"""
import os
import subprocess
import time
from pathlib import Path

import pytest
from awgit import config_integrity as ci
from awgit import worktree as awwt


def _git(*a, cwd):
    return subprocess.run(["git", *a], cwd=str(cwd), check=True, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv(ci.BACKUPS_ENV, str(tmp_path / "bk"))
    monkeypatch.setenv(ci.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("AWGIT_DECIDE_DB", str(tmp_path / "decide.db"))
    r = tmp_path / "r"
    r.mkdir()
    _git("init", "-q", cwd=r)
    _git("config", "user.email", "t@example.invalid", cwd=r)
    _git("config", "user.name", "t", cwd=r)
    _git("commit", "-q", "--allow-empty", "-m", "a", cwd=r)
    return r


def test_classify_names_the_crash_shape():
    assert ci.classify_bytes(None)[0] == ci.MISSING
    assert ci.classify_bytes(b"")[0] == ci.EMPTY
    assert ci.classify_bytes(bytes(30949))[0] == ci.ZEROED
    assert ci.classify_bytes(b"[core]\n\0\0")[0] == ci.UNPARSEABLE
    assert ci.classify_bytes(b"[core]\n\tbare = false\n")[0] == ci.OK


def test_zeroed_config_is_restored_byte_for_byte_and_kept(repo):
    common = repo / ".git"
    good = (common / "config").read_bytes()
    assert ci.sweep([common])[0]["action"] == "snapshot"
    (common / "config").write_bytes(bytes(len(good)))
    row = ci.sweep([common], fix=True)[0]
    assert row["action"] == "restored", row
    assert (common / "config").read_bytes() == good
    kept = list(common.glob("config.zeroed-*"))
    assert kept and kept[0].read_bytes() == bytes(len(good))
    _git("status", cwd=repo)  # git works again
    assert Path(os.environ[ci.LEDGER_ENV]).read_text().count('"action": "restored"') == 1


def test_without_fix_nothing_is_written(repo):
    common = repo / ".git"
    ci.sweep([common])
    (common / "config").write_bytes(bytes(100))
    row = ci.sweep([common])[0]
    assert row["action"] == "needs-fix" and (common / "config").read_bytes() == bytes(100)


def test_no_snapshot_refuses_and_never_synthesizes(repo):
    common = repo / ".git"
    (common / "config").write_bytes(bytes(100))
    row = ci.sweep([common], fix=True)[0]
    assert row["action"] == "refused" and "NOT synthesizing" in row["restore_why"]
    assert (common / "config").read_bytes() == bytes(100)
    assert ci.exit_code([row]) == 1


def test_a_live_git_lock_defers_and_a_stale_one_is_taken(repo):
    common = repo / ".git"
    ci.sweep([common])
    (common / "config").write_bytes(bytes(100))
    lock = common / "config.lock"
    lock.write_bytes(b"")
    assert ci.sweep([common], fix=True)[0]["action"] == "refused"
    old = time.time() - ci.STALE_LOCK_S - 5
    os.utime(lock, (old, old))
    assert ci.sweep([common], fix=True)[0]["action"] == "restored"
    assert not lock.exists()


def test_only_the_newest_five_distinct_snapshots_are_kept(repo):
    common = repo / ".git"
    for i in range(8):
        _git("config", "test.n", str(i), cwd=repo)
        ci.sweep([common])
    ci.sweep([common])  # unchanged bytes: no new snapshot
    snaps = ci.snapshots(common)
    assert len(snaps) == ci.KEEP
    assert b"n = 7" in snaps[0].read_bytes()


def test_discover_follows_worktrees_back_to_their_repo(repo, tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    _git("worktree", "add", "-q", "--detach", str(root / "a"), cwd=repo)
    found = ci.discover(cwd=tmp_path, roots=[root])
    assert [ci._norm(p) for p in found] == [ci._norm(repo / ".git")]


def test_stale_branch_sections_are_dropped_and_others_kept():
    text = ('[core]\n\tbare = false\n[branch "gone"]\n\tremote = origin\n'
            '[branch "kept"]\n\tremote = origin\n[remote "origin"]\n\turl = x\n')
    new, dropped = ci.stale_branch_sections(text, lambda b: b == "kept")
    assert dropped == ["gone"]
    assert '[branch "kept"]' in new and '[remote "origin"]' in new and "gone" not in new


def test_awgit_worktrees_do_not_write_the_shared_config(repo):
    _git("update-ref", "refs/remotes/origin/develop", "HEAD", cwd=repo)
    before = (repo / ".git" / "config").read_bytes()
    ok, msg, _ = awwt.create("agent-x", cwd=repo, at="origin/develop", parent=repo.parent / "wts")
    assert ok, msg
    assert (repo / ".git" / "config").read_bytes() == before


def test_crash_sweep_puts_back_zeroed_tracked_files_index_and_state(repo, tmp_path):
    """2026-11-14: the same bugcheck zeroed 1,113 tracked files and a ~/.aither index."""
    f = repo / "a.txt"
    f.write_text("hello\n")
    _git("add", "a.txt", cwd=repo)
    _git("commit", "-q", "-m", "b", cwd=repo)
    f.write_bytes(bytes(6))
    home = tmp_path / "home"
    home.mkdir()
    (home / "index.json").write_bytes(bytes(40))
    (home / "index.json.bak").write_text('{"ok": true}')
    (home / "orphan.json").write_bytes(bytes(10))
    now = time.time()
    res = ci.zero_scan([repo / ".git"], fix=False, boot=now, aither_home=home)
    kinds = sorted(x["kind"] for x in res["found"])
    assert kinds == ["state", "state", "tracked"], res
    assert f.read_bytes() == bytes(6)  # read-only without fix
    idx = repo / ".git" / "index"
    idx.write_bytes(bytes(idx.stat().st_size))
    res = ci.zero_scan([repo / ".git"], fix=True, boot=now, aither_home=home)
    acts = {x["kind"]: x["action"] for x in res["found"] if x["kind"] != "state"}
    assert acts["git-index"] == "rebuilt from HEAD", res
    assert f.read_text() == "hello\n"
    assert (home / "index.json").read_text() == '{"ok": true}'
    assert (home / "orphan.json").read_bytes() == bytes(10)  # no .bak: reported, never guessed
    assert res["complete"]
