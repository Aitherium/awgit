"""Commit guards that must be installed everywhere agents commit (2026-10-21).

In a working tree ~10 agents share, each committing through a private
GIT_INDEX_FILE, commit e06f25b0 was made from an index read before 53715854 and
silently reverted all of it (22 files deleted, others modified back). Nothing
caught it: awgit's hooks had never been installed there, `awgit hooks install`
did not install the mass-delete guard at all (a hand-written fragment allowlist
had forgotten it), and the lease gate rejected every commit in a repo whose
agents never take leases, so it could not be left on.

These tests pin the four fixes: the stale-revert guard, an installer that ships
every fragment, `awgit hooks ensure`, and a lease gate that only enforces where
leases are in use.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))

from awgit import bridge  # noqa: E402
from awgit.cli import main  # noqa: E402

GUARD = PKG_ROOT / "awgit" / "hooks" / "pre-commit.d" / "vcs-stale-revert-guard"


def _sh() -> str:
    found = shutil.which("sh")
    if found:
        return found
    exec_path = subprocess.run(["git", "--exec-path"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace").stdout.strip()
    ep = Path(exec_path)
    for cand in (ep.parents[2] / "bin" / "sh.exe", ep.parents[1] / "bin" / "sh.exe"):
        if cand.is_file():
            return str(cand)
    pytest.skip("no POSIX sh to run hook fragments")


def _env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_INDEX_FILE", "AITHER_ALLOW_REVERT", "VCS_LEASES_ENFORCE",
                        "AITHER_ALLOW_MASS_DELETE", "GIT_DIR", "GIT_WORK_TREE")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", AITHER_ACTOR="test-actor")
    env.update(extra)
    return env


def _git(repo: Path, *args: str, env: dict | None = None, check: bool = True):
    p = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env or _env(),
                       stdin=subprocess.DEVNULL)
    if check and p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {p.stdout}{p.stderr}")
    return p


def _commit_all(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _guard(repo: Path, env: dict | None = None):
    return subprocess.run([_sh(), str(GUARD)], cwd=str(repo), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env or _env(),
                          stdin=subprocess.DEVNULL)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "shared"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "core.autocrlf", "false")
    (r / "a.txt").write_text("a1\n")
    (r / "b.txt").write_text("b1\n")
    _commit_all(r, "base")
    return r


def _peer_commit(repo: Path) -> str:
    """Another agent's commit: adds c.txt and modifies a.txt."""
    (repo / "a.txt").write_text("a2 peer\n")
    (repo / "c.txt").write_text("c peer\n")
    return _commit_all(repo, "peer work")


def _stale_index(repo: Path, tmp_path: Path, base: str) -> dict:
    """A private index read from `base` (before the peer's commit), plus one own edit."""
    idx = tmp_path / "private.index"
    env = _env(GIT_INDEX_FILE=str(idx))
    _git(repo, "read-tree", base, env=env)
    (repo / "mine.txt").write_text("mine\n")
    _git(repo, "add", "mine.txt", env=env)
    return env


# -- the stale-revert guard --------------------------------------


def test_stale_private_index_is_blocked(repo: Path, tmp_path: Path):
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    peer = _peer_commit(repo)
    env = _stale_index(repo, tmp_path, base)
    p = _guard(repo, env)
    assert p.returncode == 1, p.stderr
    assert "COMMIT BLOCKED" in p.stderr
    assert "a.txt" in p.stderr and "c.txt" in p.stderr, p.stderr
    assert peer[:8] in p.stderr
    assert "mine.txt" not in p.stderr


def test_fresh_private_index_passes(repo: Path, tmp_path: Path):
    _peer_commit(repo)
    idx = tmp_path / "fresh.index"
    env = _env(GIT_INDEX_FILE=str(idx))
    _git(repo, "read-tree", "HEAD", env=env)
    (repo / "mine.txt").write_text("mine\n")
    _git(repo, "add", "mine.txt", env=env)
    p = _guard(repo, env)
    assert p.returncode == 0, p.stderr


def test_normal_edit_of_a_recent_file_passes(repo: Path):
    _peer_commit(repo)
    (repo / "a.txt").write_text("a3 mine\n")
    _git(repo, "add", "a.txt")
    assert _guard(repo).returncode == 0


def test_root_commit_passes(tmp_path: Path):
    r = tmp_path / "empty"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "x.txt").write_text("x\n")
    _git(r, "add", "x.txt")
    assert _guard(r).returncode == 0


def test_override_allows_a_deliberate_revert(repo: Path, tmp_path: Path):
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _peer_commit(repo)
    env = _stale_index(repo, tmp_path, base)
    env["AITHER_ALLOW_REVERT"] = "1"
    assert _guard(repo, env).returncode == 0


def test_git_rm_of_a_just_added_file_passes_but_rm_cached_is_blocked(repo: Path):
    _peer_commit(repo)
    _git(repo, "rm", "-q", "c.txt")          # gone from disk too: deliberate
    assert _guard(repo).returncode == 0
    _git(repo, "reset", "-q", "HEAD", "--", "c.txt")
    _git(repo, "checkout", "--", "c.txt")
    _git(repo, "rm", "-q", "--cached", "c.txt")  # still on disk: the stale-index shape
    p = _guard(repo)
    assert p.returncode == 1 and "c.txt" in p.stderr


def _peer_adds_tool(repo: Path) -> None:
    (repo / "tools" / "old-name").mkdir(parents=True)
    (repo / "tools" / "old-name" / "run.py").write_text("print('run')\n")
    (repo / "tools" / "old-name" / "lib.py").write_text("X = 1\n")
    _commit_all(repo, "peer adds a tool")


def test_rename_of_recently_added_files_passes(repo: Path, tmp_path: Path):
    # 2026-10-21: a pure move of just-added files was blocked as a revert. Done through a
    # private index in a shared tree, so the OLD paths can still be on disk.
    _peer_adds_tool(repo)
    env = _env(GIT_INDEX_FILE=str(tmp_path / "mv.index"))
    _git(repo, "read-tree", "HEAD", env=env)
    new = repo / "tools" / "new-name"
    new.mkdir()
    shutil.copy(repo / "tools" / "old-name" / "run.py", new / "run.py")
    (new / "lib.py").write_text("X = 2  # moved AND edited\n")
    _git(repo, "rm", "-q", "--cached", "-r", "tools/old-name", env=env)
    _git(repo, "add", "tools/new-name", env=env)
    p = _guard(repo, env)
    assert p.returncode == 0, p.stderr


def test_git_mv_of_recently_added_files_passes(repo: Path):
    _peer_adds_tool(repo)
    _git(repo, "mv", "tools/old-name", "tools/new-name")
    assert _guard(repo).returncode == 0


def test_stale_index_undoing_a_peer_rename_is_still_blocked(repo: Path, tmp_path: Path):
    _peer_adds_tool(repo)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "mv", "tools/old-name", "tools/new-name")
    _git(repo, "commit", "-q", "--no-verify", "-m", "peer renames")
    env = _stale_index(repo, tmp_path, base)
    p = _guard(repo, env)
    assert p.returncode == 1 and "tools/old-name/run.py" in p.stderr, p.stderr


def test_stale_revert_older_than_the_window_is_not_judged(repo: Path, tmp_path: Path):
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _peer_commit(repo)
    for i in range(3):
        (repo / f"n{i}.txt").write_text(str(i))
        _commit_all(repo, f"n{i}")
    env = _stale_index(repo, tmp_path, base)
    env["AITHER_REVERT_WINDOW"] = "2"
    # Only the last 2 commits are "recent"; they never touched a.txt/c.txt, and
    # the n*.txt deletions are deletions of files still on disk -> blocked for those.
    p = _guard(repo, env)
    assert "a.txt" not in p.stderr and "c.txt" not in p.stderr


# -- the installer -----------------------------------------------


def test_every_shipped_pre_commit_fragment_is_installed(repo: Path):
    rep = bridge.ensure_hooks(str(repo))
    assert rep["status"] == "installed", rep
    frag_dir = Path(str(rep["hooks_dir"])) / "pre-commit.d"
    names = {f.name for f in frag_dir.iterdir()}
    assert {"vcs-lease-check", "vcs-mass-delete-guard", "vcs-stale-revert-guard"} <= names
    assert set(bridge._FRAGMENTS["pre-commit"]) <= names


def test_ensure_preserves_a_foreign_hook_and_is_idempotent(repo: Path):
    hooks = Path(_git(repo, "rev-parse", "--git-path", "hooks").stdout.strip())
    if not hooks.is_absolute():
        hooks = repo / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    lfs = "#!/bin/sh\n# git lfs post-commit stand-in\nexit 0\n"
    (hooks / "post-commit").write_text(lfs)
    first = bridge.ensure_hooks(str(repo))
    assert "post-commit.org" in first["preserved"]
    assert (hooks / "post-commit.org").read_text() == lfs
    assert bridge.ensure_hooks(str(repo))["status"] == "current"
    # `git lfs install` rewriting the same body over the chain is absorbed, not a conflict
    (hooks / "post-commit").write_text(lfs)
    again = bridge.ensure_hooks(str(repo))
    assert again["status"] == "upgraded" and not again["conflicts"], again
    # a DIFFERENT foreign body with an .org present is left alone and reported
    (hooks / "post-commit").write_text("#!/bin/sh\necho other\n")
    clash = bridge.ensure_hooks(str(repo))
    assert clash["status"] == "conflict"
    assert (hooks / "post-commit").read_text() == "#!/bin/sh\necho other\n"


def test_ensure_upgrades_a_stale_fragment(repo: Path):
    rep = bridge.ensure_hooks(str(repo))
    frag = Path(str(rep["hooks_dir"])) / "pre-commit.d" / "vcs-stale-revert-guard"
    frag.write_text("#!/bin/sh\n# old prototype\nexit 0\n")
    up = bridge.ensure_hooks(str(repo))
    assert up["status"] == "upgraded"
    assert "pre-commit.d/vcs-stale-revert-guard" in up["changed"]
    assert frag.read_bytes().replace(b"\r\n", b"\n") == GUARD.read_bytes().replace(b"\r\n", b"\n")


def test_linked_worktree_installs_into_the_common_hooks_dir(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt))
    rep = bridge.ensure_hooks(str(wt))
    common = Path(_git(repo, "rev-parse", "--git-path", "hooks").stdout.strip())
    if not common.is_absolute():
        common = repo / common
    assert Path(str(rep["hooks_dir"])).resolve() == common.resolve()


def test_core_hooks_path_is_skipped(repo: Path):
    _git(repo, "config", "core.hooksPath", ".husky")
    rep = bridge.ensure_hooks(str(repo))
    assert rep["status"] == "skipped" and "core.hooksPath" in str(rep["reason"])


def test_cli_ensure_scan_reports_each_repo_once(repo: Path, tmp_path: Path, capsys):
    _git(repo, "worktree", "add", "-q", str(tmp_path / "wt2"))
    (tmp_path / "plain").mkdir()
    rc = main(["hooks", "ensure", "--scan", str(tmp_path), "--depth", "1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("awgit hooks:") == 1, out
    assert "installed" in out


def test_installed_chain_blocks_a_real_stale_commit(repo: Path, tmp_path: Path):
    bridge.ensure_hooks(str(repo))
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _peer_commit(repo)
    env = _stale_index(repo, tmp_path, base)
    p = _git(repo, "commit", "-q", "-m", "mine", env=env, check=False)
    assert p.returncode != 0
    assert "COMMIT BLOCKED" in p.stderr
    head_files = _git(repo, "ls-tree", "--name-only", "HEAD").stdout.split()
    assert "c.txt" in head_files and "mine.txt" not in head_files


# -- the lease gate ----------------------------------------------


def _lease_check(repo: Path, monkeypatch, **env: str) -> int:
    for k in ("VCS_LEASES_ENFORCE",):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("AITHER_ACTOR", "test-actor")
    monkeypatch.chdir(repo)
    monkeypatch.delenv("VCS_REPO_ROOT", raising=False)
    return main(["lease-check"])


def test_lease_gate_is_report_only_without_leases_in_this_repo(repo: Path, monkeypatch, capsys):
    (repo / "x.py").write_text("x = 1\n")
    _git(repo, "add", "x.py")
    assert _lease_check(repo, monkeypatch, VCS_LEASES_ENFORCE="1") == 0
    assert "REPORT-ONLY" in capsys.readouterr().err


def test_lease_gate_enforces_when_the_repo_opts_in(repo: Path, monkeypatch):
    _git(repo, "config", "awgit.leases", "enforce")
    (repo / "x.py").write_text("x = 1\n")
    _git(repo, "add", "x.py")
    assert _lease_check(repo, monkeypatch) == 1


def test_lease_gate_enforces_when_a_live_lease_names_a_path_here(repo: Path, monkeypatch):
    from awgit.leases import LeaseRegistry

    (repo / "x.py").write_text("x = 1\n")
    _git(repo, "add", "x.py")
    monkeypatch.chdir(repo)
    LeaseRegistry().acquire("someone-else", ["a.txt"], kind="path")
    assert _lease_check(repo, monkeypatch, VCS_LEASES_ENFORCE="1") == 1


def test_lease_gate_off_in_repo_config_is_silent(repo: Path, monkeypatch):
    _git(repo, "config", "awgit.leases", "off")
    (repo / "x.py").write_text("x = 1\n")
    _git(repo, "add", "x.py")
    assert _lease_check(repo, monkeypatch, VCS_LEASES_ENFORCE="1") == 0
