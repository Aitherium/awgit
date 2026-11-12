"""awgit deploy-tree: agent edits into a marked deploy tree are refused, everything else passes."""
import io
import json
import subprocess

import pytest
from awgit import deploy_tree as dt


def _repo(path, marked):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if marked:
        subprocess.run(["git", "-C", str(path), "config", "awgit.deploytree", "true"], check=True)
    dt._cache.clear()
    return path


def _run(payload, env=None):
    out = io.StringIO()
    dt._cache.clear()
    old = dt.os.environ.get(dt.OVERRIDE_ENV)
    if env is not None:
        dt.os.environ[dt.OVERRIDE_ENV] = env
    try:
        assert dt.guard_main(io.StringIO(json.dumps(payload)), out) == 0
    finally:
        if env is not None:
            if old is None:
                dt.os.environ.pop(dt.OVERRIDE_ENV, None)
            else:
                dt.os.environ[dt.OVERRIDE_ENV] = old
    return json.loads(out.getvalue()) if out.getvalue() else None


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
