"""awgit deploy-tree: agent edits into a marked deploy tree are refused, everything else passes.

The same hook also refuses a loose script with no git work tree to own it
(a second, independent rule -- see :data:`awgit.deploy_tree.SCRIPT_SUFFIXES`).
"""
import io
import json
import os
import subprocess
from pathlib import Path

import pytest
from awgit import deploy_tree as dt


def _repo(path, marked):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if marked:
        subprocess.run(["git", "-C", str(path), "config", "awgit.deploytree", "true"], check=True)
    dt._cache.clear()
    dt._wt_cache.clear()
    return path


def _run(payload, env=None, loose_env=None):
    out = io.StringIO()
    dt._cache.clear()
    dt._wt_cache.clear()
    saved = {k: os.environ.get(k) for k in (dt.OVERRIDE_ENV, dt.LOOSE_OVERRIDE_ENV)}
    if env is not None:
        os.environ[dt.OVERRIDE_ENV] = env
    if loose_env is not None:
        os.environ[dt.LOOSE_OVERRIDE_ENV] = loose_env
    try:
        assert dt.guard_main(io.StringIO(json.dumps(payload)), out) == 0
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return json.loads(out.getvalue()) if out.getvalue() else None


def _not_temp(monkeypatch, *roots):
    """Pin what counts as the system temp dir so a test does not depend on where
    pytest put ``tmp_path`` (usually under %TEMP%)."""
    monkeypatch.setattr(dt, "_temp_roots", lambda env=None: [Path(r) for r in roots])



TOOLS = [("Edit", "file_path"), ("Write", "file_path"),
         ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")]


@pytest.mark.parametrize("tool,key", TOOLS)
def test_write_into_marked_tree_is_denied(tmp_path, tool, key):
    tree = _repo(tmp_path / "deploy", marked=True)
    got = _run({"tool_name": tool, "tool_input": {key: str(tree / "a" / "b.py")}})
    assert got["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "awgit wt new" in got["hookSpecificOutput"]["permissionDecisionReason"]


def test_unmarked_tree_and_reads_pass(tmp_path):
    plain = _repo(tmp_path / "plain", marked=False)
    assert _run({"tool_name": "Edit", "tool_input": {"file_path": str(plain / "x.py")}}) is None
    tree = _repo(tmp_path / "deploy", marked=True)
    assert _run({"tool_name": "Read", "tool_input": {"file_path": str(tree / "x.py")}}) is None


def test_relative_path_resolves_against_cwd(tmp_path):
    tree = _repo(tmp_path / "deploy", marked=True)
    payload = {"tool_name": "Write", "cwd": str(tree), "tool_input": {"file_path": "sub/new.txt"}}
    got = _run(payload)
    assert got["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_nested_linked_worktree_is_its_own_checkout(tmp_path):
    tree = _repo(tmp_path / "deploy", marked=True)
    (tree / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(tree), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(tree), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "base"], check=True)
    wt = tree / "wt" / "mine"
    subprocess.run(["git", "-C", str(tree), "worktree", "add", "-q", "-b", "mine", str(wt)],
                   check=True)
    assert _run({"tool_name": "Edit", "tool_input": {"file_path": str(wt / "f.txt")}}) is None


def test_override_and_garbage_payload_pass(tmp_path):
    tree = _repo(tmp_path / "deploy", marked=True)
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(tree / "x")}}
    assert _run(payload, env="1") is None
    out = io.StringIO()
    assert dt.guard_main(io.StringIO("not json"), out) == 0 and out.getvalue() == ""


def test_status_counts_dirty_files(tmp_path):
    tree = _repo(tmp_path / "deploy", marked=True)
    (tree / "d").mkdir()
    (tree / "d" / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(tree), "add", "d/f.txt"], check=True)
    st = dt.status(tree)
    assert st["marked"] is True and st["dirty"] == 1 and st["by_top"] == {"d": 1}


def test_scratch_and_gitignored_paths_pass(tmp_path):
    tree = _repo(tmp_path / "deploy", marked=True)
    (tree / ".gitignore").write_text("logs/\n")
    for rel in (".scratch/notes.md", "logs/run.log"):
        assert _run({"tool_name": "Write", "tool_input": {"file_path": str(tree / rel)}}) is None
    got = _run({"tool_name": "Write", "tool_input": {"file_path": str(tree / "src" / "a.py")}})
    assert got["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- second rule: a NEW loose script with no git work tree to own it ---------

def test_loose_script_under_temp_is_denied(tmp_path, monkeypatch):
    temp = tmp_path / "systmp"
    temp.mkdir()
    _not_temp(monkeypatch, temp)
    # A stale copy already on disk is still a loose script nothing owns: the
    # acceptance case was a temp script written the day before the guard.
    script = temp / "kokoro-check.sh"
    script.write_text("#!/bin/sh\n")
    got = _run({"tool_name": "Write", "tool_input": {"file_path": str(script)}})
    assert got["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = got["hookSpecificOutput"]["permissionDecisionReason"]
    assert "awgit wt new" in reason
    assert "awsh_awrun_queue" in reason
    assert "mediaforge_run_op" in reason
    assert "under the system temp dir" in reason


def test_new_script_outside_repo_is_denied(tmp_path, monkeypatch):
    _not_temp(monkeypatch)  # not temp -> the "outside any repo" wording
    d = tmp_path / "media" / "shoot" / "v10.1"
    d.mkdir(parents=True)
    got = _run({"tool_name": "Write", "tool_input": {"file_path": str(d / "render.sh")}})
    assert got["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = got["hookSpecificOutput"]["permissionDecisionReason"]
    assert "awgit wt new" in reason and "outside any git work tree" in reason


def test_new_script_in_worktree_is_allowed(tmp_path):
    repo = _repo(tmp_path / "repo", marked=False)
    assert _run({"tool_name": "Write",
                 "tool_input": {"file_path": str(repo / "tool.py")}}) is None
    assert _run({"tool_name": "Write",
                 "tool_input": {"file_path": str(repo / ".scratch" / "x.py")}}) is None


def test_new_script_in_scratch_outside_repo_is_allowed(tmp_path, monkeypatch):
    _not_temp(monkeypatch, tmp_path)
    assert _run({"tool_name": "Write",
                 "tool_input": {"file_path": str(tmp_path / ".scratch" / "probe.py")}}) is None


def test_data_extension_under_temp_is_allowed(tmp_path, monkeypatch):
    temp = tmp_path / "systmp"
    temp.mkdir()
    _not_temp(monkeypatch, temp)
    assert _run({"tool_name": "Write",
                 "tool_input": {"file_path": str(temp / "fixture.json")}}) is None


def test_existing_loose_script_outside_repo_is_denied(tmp_path, monkeypatch):
    _not_temp(monkeypatch)
    f = tmp_path / "installed" / "tool.sh"
    f.parent.mkdir()
    f.write_text("#!/bin/sh\n")
    got = _run({"tool_name": "Edit", "tool_input": {"file_path": str(f)}})
    assert got["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _run({"tool_name": "Edit", "tool_input": {"file_path": str(f)}},
                loose_env="1") is None


def test_loose_override_allows_script(tmp_path, monkeypatch):
    _not_temp(monkeypatch, tmp_path)
    assert _run({"tool_name": "Write",
                 "tool_input": {"file_path": str(tmp_path / "oneoff.sh")}},
                loose_env="1") is None


def test_relative_loose_script_resolves_against_cwd(tmp_path, monkeypatch):
    _not_temp(monkeypatch)
    got = _run({"tool_name": "Write", "cwd": str(tmp_path),
                "tool_input": {"file_path": "render.sh"}})
    assert got["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_reason_names_cwd_repo_when_available(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "workrepo", marked=False)
    _not_temp(monkeypatch)
    loose = tmp_path / "loose"
    loose.mkdir()
    got = _run({"tool_name": "Write", "cwd": str(repo),
                "tool_input": {"file_path": str(loose / "x.py")}})
    assert str(repo) in got["hookSpecificOutput"]["permissionDecisionReason"]


def test_tracked_script_in_worktree_is_allowed(tmp_path):
    repo = _repo(tmp_path / "tracked", marked=False)
    f = repo / "dev" / "check_x.py"
    f.parent.mkdir()
    f.write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "dev/check_x.py"], check=True)
    assert _run({"tool_name": "Edit", "tool_input": {"file_path": str(f)}}) is None


def test_worktree_of_finds_root_and_none(tmp_path):
    repo = _repo(tmp_path / "r", marked=False)
    assert str(dt.worktree_of(repo / "a" / "b.py")).lower() == str(repo).lower()
    assert dt.worktree_of(tmp_path / "norepo" / "x.py") is None
