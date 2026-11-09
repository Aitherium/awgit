"""Capture parses the node registry once per range run and writes it once per commit.

Measured 2026-11-08: ``nodes.json`` is 88 MB (336k nodes, ~379 MB parsed). Each
captured commit re-parsed it, and every new symbol rewrote the whole file, so
``capture --range --budget 60`` peaked at 583 MB after the op-log fix.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit import cli, nodeid  # noqa: E402
from awgit.capture import capture_ops  # noqa: E402
from awgit.nodeid import StableNodeIDManager  # noqa: E402


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def commit(repo: Path, name: str, body: str) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t",
        "commit", "-q", "--no-verify", "-m", f"touch {name}")
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "commit.gpgsign", "false")
    commit(r, "base.txt", "root\n")
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "vcs"))
    monkeypatch.chdir(r)
    return r


def five_functions(tag: str) -> str:
    return "".join(f"def {tag}{i}():\n    return {i}\n\n" for i in range(5))


def count_registry_writes(monkeypatch) -> dict:
    calls = {"n": 0}
    real = nodeid.os.replace

    def counting(src, dst):
        if Path(dst).name == "nodes.json":
            calls["n"] += 1
        return real(src, dst)

    monkeypatch.setattr(nodeid.os, "replace", counting)
    return calls


def count_registry_loads(monkeypatch) -> dict:
    calls = {"n": 0}
    real = StableNodeIDManager._load

    def counting(self):
        calls["n"] += 1
        return real(self)

    monkeypatch.setattr(StableNodeIDManager, "_load", counting)
    return calls


def test_one_registry_write_per_commit(repo: Path, tmp_path: Path, monkeypatch):
    writes = count_registry_writes(monkeypatch)
    sha = commit(repo, "m.py", five_functions("f"))
    op = capture_ops(sha, actor="t", repo_path=str(repo), data_root=tmp_path / "vcs")
    assert op is not None and len(op.node_changes) == 5
    assert writes["n"] == 1, f"nodes.json written {writes['n']} times for one commit"
    reg = json.loads((tmp_path / "vcs" / "nodes.json").read_text(encoding="utf-8"))
    assert len(reg["nodes"]) == 5


def test_range_run_parses_the_registry_once(repo: Path, tmp_path: Path, monkeypatch, capsys):
    commit(repo, "a.py", five_functions("a"))
    commit(repo, "README.md", "docs only -- must not load the registry\n")
    commit(repo, "b.py", five_functions("b"))
    commit(repo, "c.py", five_functions("c"))
    loads = count_registry_loads(monkeypatch)
    assert cli.main(["capture", "--range", "HEAD"]) == 0
    assert "3 recorded" in capsys.readouterr().out
    # the first commit creates the file (nothing to load); later ones reuse it
    assert loads["n"] == 0, f"registry parsed {loads['n']} times in one run"
    reg = json.loads((tmp_path / "vcs" / "nodes.json").read_text(encoding="utf-8"))
    assert len(reg["nodes"]) == 15


def test_a_peer_write_between_commits_is_not_overwritten(repo: Path, tmp_path: Path):
    data = tmp_path / "vcs"
    cache: dict = {}
    s1 = commit(repo, "a.py", five_functions("a"))
    assert capture_ops(s1, actor="t", repo_path=str(repo), data_root=data,
                       node_cache=cache) is not None
    # a peer process (post-commit hook) registers a node in between
    peer = StableNodeIDManager(path=data / "nodes.json", persist=True)
    peer_id = peer.id_for("peer_symbol", "peer.py", "function")
    s2 = commit(repo, "b.py", five_functions("b"))
    assert capture_ops(s2, actor="t", repo_path=str(repo), data_root=data,
                       node_cache=cache) is not None
    reg = json.loads((data / "nodes.json").read_text(encoding="utf-8"))
    assert peer_id in reg["nodes"], "a cached registry overwrote a peer's node"
    assert len(reg["nodes"]) == 11


def test_streamed_save_is_byte_identical_to_json_dumps(tmp_path: Path):
    path = tmp_path / "nodes.json"
    mgr = StableNodeIDManager(path=path, persist=True)
    mgr.id_for("ünïcode", "p.py", "function")
    sid = mgr.id_for("old", "q.py", "function")
    mgr.rename_node(sid, "new")
    mgr.add_redirect("node_gone", sid)
    payload = {"nodes": mgr._nodes, "redirects": mgr._redirects, "aliases": mgr._aliases}
    assert path.read_text(encoding="utf-8") == json.dumps(payload, ensure_ascii=False)
    again = StableNodeIDManager(path=path, persist=True)
    assert again.id_for("new", "q.py") == sid
