"""The op-log stays linear in size and cheap to open.

Measured 2026-11-08: ``oplog.jsonl`` was 989 MB for 12,395 ops, 860 MB of it
``parent_ops``. Capture recorded EVERY captured first-parent ancestor per op
(quadratic growth), and every ``OpLog(...)`` parsed every op, so the hourly
``capture --range`` held 2.3-3.3 GB. These pin the fix:

* capture records only the NEAREST captured ancestor;
* ``linearize`` orders by reachability, so A->B->C asked for {A, C} keeps A first;
* opening a log, ``has_commit``, ``sha_index`` and ``append`` parse no op;
* lines an older awgit appends without the index are still seen;
* ``awgit oplog compact`` reduces an old-shape log with the same causal closure,
  keeps a backup, and leaves a log the old line-by-line reader still parses.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Set

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit import cli, schema  # noqa: E402
from awgit.capture import capture_ops  # noqa: E402
from awgit.oplog import OpLog  # noqa: E402
from awgit.schema import EditOp  # noqa: E402


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def commit(repo: Path, name: str, body: str) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t",
        "commit", "-q", "--no-verify", "-m", f"touch {name}")
    return git(repo, "rev-parse", "HEAD").strip()


def op(op_id: str, parents: List[str], sha: str = "") -> EditOp:
    return EditOp(op_id=op_id, parent_ops=parents, actor="t", ts="2026-11-08T00:00:00",
                  git_sha=sha or f"sha-{op_id}", git_parent_sha="p",
                  file_paths=["f.py"], node_changes=[], summary="s")


def write_old_style(path: Path, ops: List[EditOp]) -> None:
    """What an older awgit wrote: a header, then one to_dict line per op, no index."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# vcs-oplog v1\n")
        for o in ops:
            f.write(json.dumps(o.to_dict()) + "\n")


def old_reader(path: Path) -> List[EditOp]:
    """The pre-index ``OpLog._load``: every non-comment line is one EditOp."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(EditOp.from_dict(json.loads(line)))
    return out


def closures(ops: List[EditOp]) -> Dict[str, Set[str]]:
    graph = {o.op_id: list(o.parent_ops) for o in ops}
    out: Dict[str, Set[str]] = {}
    for oid in graph:
        seen: Set[str] = set()
        stack = list(graph[oid])
        while stack:
            x = stack.pop()
            if x not in seen:
                seen.add(x)
                stack.extend(graph.get(x, ()))
        out[oid] = seen
    return out


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "commit.gpgsign", "false")
    commit(r, "base.txt", "root\n")
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "vcs"))
    return r


def test_capture_records_only_the_nearest_captured_ancestor(repo: Path, tmp_path: Path):
    data = tmp_path / "vcs"
    shas = [commit(repo, f"m{i}.py", f"def f{i}():\n    return {i}\n") for i in range(4)]
    ops = [capture_ops(s, actor="t", repo_path=str(repo), data_root=data) for s in shas]
    assert all(o is not None for o in ops)
    assert ops[0].parent_ops == []
    for prev, cur in zip(ops, ops[1:]):
        assert cur.parent_ops == [prev.op_id], "every captured ancestor was recorded"
    # the causal closure is still the whole chain
    log = OpLog(data_root=data)
    assert [o.op_id for o in log.linearize([ops[3].op_id, ops[0].op_id])] == [
        ops[0].op_id, ops[3].op_id]


def test_linearize_orders_through_an_unwanted_op(tmp_path: Path):
    # ids chosen so that sorted order is the REVERSE of causal order
    a, b, c = "z-a", "m-b", "a-c"
    log = OpLog(data_root=tmp_path / "vcs")
    for o in (op(a, []), op(b, [a]), op(c, [b])):
        log.append(o)
    fresh = OpLog(data_root=tmp_path / "vcs")
    assert [o.op_id for o in fresh.linearize([c, a])] == [a, c]
    assert [o.op_id for o in fresh.linearize([c, b, a])] == [a, b, c]


def test_open_has_commit_sha_index_append_parse_no_op(tmp_path: Path, monkeypatch):
    data = tmp_path / "vcs"
    write_old_style(data / "oplog.jsonl", [op(f"o{i}", [f"o{j}" for j in range(i)])
                                           for i in range(20)])
    OpLog(data_root=data)  # builds the index once

    calls = {"n": 0}
    real = EditOp.from_dict.__func__

    def counting(cls, d):
        calls["n"] += 1
        return real(cls, d)

    monkeypatch.setattr(schema.EditOp, "from_dict", classmethod(counting))
    log = OpLog(data_root=data)
    assert log.has_commit("sha-o7")
    assert not log.has_commit("sha-nope")
    assert log.sha_index()["sha-o19"] == "o19"
    log.append(op("o20", ["o19"]))
    assert OpLog(data_root=data).has_commit("sha-o20")
    assert calls["n"] == 0, f"{calls['n']} ops parsed for index-only calls"
    # a point lookup parses exactly the one line it needs
    assert log.get("o3").op_id == "o3"
    assert calls["n"] == 1


def test_lines_an_older_awgit_appends_are_seen(tmp_path: Path):
    data = tmp_path / "vcs"
    log = OpLog(data_root=data)
    log.append(op("new1", []))
    with open(data / "oplog.jsonl", "a", encoding="utf-8") as f:  # old writer: no index
        f.write(json.dumps(op("old1", ["new1"]).to_dict()) + "\n")
    again = OpLog(data_root=data)
    assert again.has_commit("sha-old1")
    assert again.get("old1").parent_ops == ["new1"]
    again.append(op("new2", ["old1"]))
    assert [o.op_id for o in OpLog(data_root=data).all_ops()] == ["new1", "old1", "new2"]
    assert [o.op_id for o in old_reader(data / "oplog.jsonl")] == ["new1", "old1", "new2"]


def test_a_rewritten_log_invalidates_the_index(tmp_path: Path):
    data = tmp_path / "vcs"
    log = OpLog(data_root=data)
    for i in range(3):
        log.append(op(f"x{i}", []))
    # replace the log behind the index (as a restore or compaction would)
    write_old_style(data / "oplog.jsonl", [op("y0", []), op("y1", ["y0"])])
    fresh = OpLog(data_root=data)
    assert not fresh.has_commit("sha-x0")
    assert fresh.has_commit("sha-y1")
    assert fresh.get("y1").parent_ops == ["y0"]
    # and a live instance whose offsets went stale re-indexes on lookup
    assert log.get("x1") is None or log.get("x1").op_id == "x1"


def test_compact_reduces_with_the_same_closure_and_keeps_a_backup(tmp_path: Path, capsys):
    data = tmp_path / "vcs"
    # an old-shape chain (every ancestor listed, closest first) plus a merge-ish
    # op with two independent parents and a dangling parent id
    chain = [op(f"c{i:02d}", [f"c{j:02d}" for j in range(i - 1, -1, -1)]) for i in range(30)]
    side = op("s0", [])
    join = op("j0", ["c29", "s0", "c05", "missing"])
    before = chain + [side, join]
    write_old_style(data / "oplog.jsonl", before)
    size_before = (data / "oplog.jsonl").stat().st_size
    order_before = [o.op_id for o in OpLog(data_root=data).linearize(
        [o.op_id for o in before])]

    assert cli.main(["oplog", "compact", "--dry-run", "--data-root", str(data)]) == 0
    assert (data / "oplog.jsonl").stat().st_size == size_before, "dry run changed the log"
    assert not list(data.glob("*.tmp")), "dry run left its temp file"
    capsys.readouterr()

    assert cli.main(["oplog", "compact", "--apply", "--data-root", str(data)]) == 0
    out = capsys.readouterr().out
    assert "applied" in out
    backups = list(data.glob("oplog.jsonl.bak-*-precompact"))
    assert len(backups) == 1 and backups[0].stat().st_size == size_before
    after = old_reader(data / "oplog.jsonl")  # the old reader still parses it
    assert [o.op_id for o in after] == [o.op_id for o in before]
    by_id = {o.op_id: o for o in after}
    assert by_id["c29"].parent_ops == ["c28"]
    assert by_id["c00"].parent_ops == []
    assert sorted(by_id["j0"].parent_ops) == ["c29", "missing", "s0"]
    assert closures(after) == closures(before)
    assert (data / "oplog.jsonl").stat().st_size < size_before
    log = OpLog(data_root=data)
    assert [o.op_id for o in log.linearize([o.op_id for o in before])] == order_before
    assert log.has_commit("sha-c17") and log.get("c17").parent_ops == ["c16"]
    # idempotent: a second compaction changes nothing but still verifies
    assert cli.main(["oplog", "compact", "--dry-run", "--data-root", str(data)]) == 0
