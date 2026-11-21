"""awgit integrity: a zeroed .git/config comes back byte for byte, under git's lock.

2026-11-13 and 2026-11-14 the deploy tree's .git/config was found all NUL bytes at its
old size after the host crashed (git renames config.lock into place and never fsyncs
it). These pin what the guard may and may not do about that.
"""
import json
import os
import subprocess
import time
import types
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


def _fake_clock(monkeypatch, step):
    """time.monotonic advances ``step`` s per call: the budget runs out mid-walk."""
    t = [1000.0]

    def mono():
        t[0] += step
        return t[0]
    # a module-local stand-in: patching time.monotonic itself would move pytest's clock
    monkeypatch.setattr(ci, "time", types.SimpleNamespace(time=time.time, monotonic=mono))


def _big_home(tmp_path, n_dirs=3, n_files=40):
    home = tmp_path / "home"
    for d in range(n_dirs):
        for i in range(n_files):
            p = home / f"d{d}" / f"s{i // 10}" / f"f{i}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}")
    return home


def test_budget_is_checked_inside_a_unit_not_only_between(tmp_path, monkeypatch):
    """#16946: one ~/.aither dir (harvest, 371,942 files) ran the wake past its 240 s kill
    because the budget was only checked before each unit."""
    home = _big_home(tmp_path, n_dirs=1, n_files=400)
    monkeypatch.setattr(ci, "SCAN_CHECK_EVERY", 8)
    _fake_clock(monkeypatch, 1.0)
    res = ci.zero_scan([], fix=False, boot=time.time(), aither_home=home, budget_s=30)
    assert not res["complete"]
    # stopped about at the budget, not after walking all 400 files
    assert res["seconds"] < 60, res
    # the top-level loose files were the first unit; d0 was cut short, not done
    assert "home:" + ci._norm(home) in res["done"]
    assert "home:" + ci._norm(home / "d0") not in res["done"]


def test_a_unit_that_cannot_finish_with_the_whole_budget_is_skipped_and_named(
        tmp_path, monkeypatch):
    home = _big_home(tmp_path, n_dirs=2, n_files=400)
    monkeypatch.setattr(ci, "SCAN_CHECK_EVERY", 8)
    _fake_clock(monkeypatch, 1.0)
    big = "home:" + ci._norm(home / "d0")
    done = ["home:" + ci._norm(home)]  # the next run starts with d0, its FIRST unit
    res = ci.zero_scan([], fix=False, boot=time.time(), aither_home=home, budget_s=30,
                       done=done)
    assert res["skipped"] == [big] and big in res["done"], res
    assert not res["complete"]  # d1 is still to come next tick


def test_progress_is_saved_after_each_unit_so_a_kill_keeps_it(tmp_path, monkeypatch):
    """#16946: crash-scan.json was written only when zero_scan returned; the 240 s kill
    threw every finished unit away and each run started over."""
    monkeypatch.setenv(ci.LEDGER_ENV, str(tmp_path / "aw" / "ledger.jsonl"))
    home = _big_home(tmp_path, n_dirs=3, n_files=5)
    monkeypatch.setattr(ci, "SCAN_SAVE_EVERY_S", 0.0)
    monkeypatch.setattr(ci, "boot_time", lambda: 5000.0)
    monkeypatch.setattr(ci, "unclean_boot", lambda b: (True, "bugcheck 0x00000050"))
    real = ci.zero_scan
    saved = []

    class KilledError(Exception):
        pass

    def killed_after_two_units(*a, **kw):
        user = kw["progress"]

        def spy(p):
            user(p)
            saved.append(len(p["done"]))
            if len(p["done"]) == 2:
                raise KilledError()
        kw["progress"] = spy
        return real(*a, aither_home=home, **kw)
    monkeypatch.setattr(ci, "zero_scan", killed_after_two_units)
    with pytest.raises(KilledError):
        ci.crash_scan_if_needed([], fix=False)
    st = json.loads(ci.scan_state_path().read_text())
    assert len(st["done"]) == 2 and st["complete"] is False, st
    # the next run resumes: it never re-sweeps the two saved units
    monkeypatch.setattr(ci, "zero_scan", lambda *a, **kw: real(*a, aither_home=home, **kw))
    res = ci.crash_scan_if_needed([], fix=False)
    assert res["complete"] and len(res["done"]) == 4, res


def test_found_is_not_counted_twice_when_a_unit_is_swept_again():
    a = {"kind": "state", "path": "p", "action": "reported (no .bak)"}
    b = {"kind": "state", "path": "p", "action": "restored from .bak"}
    assert ci._merge_found([a], [b]) == [b]


def test_harvest_is_a_skipped_bulk_store():
    assert "harvest" in ci.SCAN_SKIP_DIRS
    assert ci.SCAN_BUDGET_S <= 150  # well inside the wake's 240 s timeout
