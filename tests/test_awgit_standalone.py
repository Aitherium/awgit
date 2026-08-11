"""Standalone awgit smoke tests — the self-contained package, no AitherOS libs.

Gate: the vendored parser produces the SAME chunk shape the reference
CodeGraph parser produces (so node ids stay stable cross-repo); the vendored
stable-node-id manager survives reload + rename; capture turns a real commit
into an EditOp whose bodies reconstruct from the content-addressed store; and
differential sync converges. Run from the package root:

    cd awgit && python -m pytest tests/ -q
"""

from __future__ import annotations

import subprocess

import pytest
from awgit import (
    BodyStore,
    capture_ops,
    export_delta,
    import_delta,
    sync_status,
)
from awgit.nodeid import StableNodeIDManager
from awgit.parser import ChunkType, parse_source_bytes

SRC = (
    '"""Sample module."""\n'
    "\n"
    "PORT = 8150\n"
    "\n"
    "def alpha(x: int) -> int:\n"
    "    return x + 1\n"
    "\n"
    "def beta(y):\n"
    "    return y * 2\n"
    "\n"
    "class Greeter:\n"
    "    def greet(self, name: str):\n"
    "        return f'hi {name}'\n"
    "\n"
    "    async def wave(self):\n"
    "        return 'bye'\n"
)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(r), check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(r), check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(r), check=True)
    return r


def _write(repo, rel, text):
    (repo / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", rel], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", rel], cwd=str(repo), check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, text=True, check=True, encoding="utf-8",
        errors="replace",
    ).stdout.strip()


def test_parser_chunks_shape():
    """Same node universe as the reference: function/class/method(+module)."""
    result = parse_source_bytes(SRC.encode(), "m.py")
    by_name = {c.name: c for c in result.chunks}
    assert by_name["m"].chunk_type.value == ChunkType.MODULE.value
    assert by_name["alpha"].chunk_type.value == "function"
    assert by_name["alpha"].signature == "def alpha(x: int) -> int"
    assert by_name["beta"].chunk_type.value == "function"
    assert by_name["Greeter"].chunk_type.value == "class"
    # Methods are named Class.method — this keys the stable node id.
    assert by_name["Greeter.greet"].chunk_type.value == "method"
    assert by_name["Greeter.greet"].signature == "def greet(self, name: str)"
    assert by_name["Greeter.wave"].signature.startswith("async def")
    # Lines are 1-indexed, end inclusive (alpha spans "def"..body).
    alpha = by_name["alpha"]
    assert alpha.start_line == 5 and alpha.end_line == 6


def test_parser_module_chunk_conditional():
    """Module chunk only when there is a docstring or top-level consts."""
    bare = parse_source_bytes(b"def f():\n    return 1\n", "b.py")
    assert [c.name for c in bare.chunks] == ["f"]  # no module node
    with_doc = parse_source_bytes(b'"""Doc."""\ndef f():\n    return 1\n', "d.py")
    assert any(c.chunk_type.value == "module" for c in with_doc.chunks)


def test_parser_syntax_error_is_empty():
    assert parse_source_bytes(b"def f(:\n", "bad.py").chunks == []


def test_nodeid_stable_across_reload(tmp_path):
    """Stable node ids survive reload, and rename preserves the id."""
    path = tmp_path / "nodes.json"
    m1 = StableNodeIDManager(path=path, persist=True)
    sid = m1.id_for("alpha", "m.py", "function")
    m2 = StableNodeIDManager(path=path, persist=True)
    assert m2.id_for("alpha", "m.py", "function") == sid
    # rename preserves the stable id (edges survive)
    assert m2.rename_node(sid, "alpha2")
    m3 = StableNodeIDManager(path=path, persist=True)
    assert m3.by_name("alpha2") == [sid]


def test_capture_overlap_and_bodies(repo, tmp_path):
    """Two commits editing one function -> overlapping node id; bodies store."""
    data = tmp_path / "data"
    _write(repo, "m.py", SRC)
    sha = _write(repo, "m.py", SRC.replace("x + 1", "x + 10"))
    op = capture_ops(sha, actor="test", repo_path=str(repo), data_root=data)
    assert op is not None
    symbols = [nc.symbol for nc in op.node_changes]
    assert "alpha" in symbols  # the edited function
    assert op.git_sha == sha
    assert op.ledger_ref  # deterministic ledger reference stamped
    # The referenced body reconstructs from the store WITHOUT git blobs.
    store = BodyStore(data_root=data)
    alpha = next(nc for nc in op.node_changes if nc.symbol == "alpha")
    assert store.get(alpha.new_body_sha) is not None


def test_sync_roundtrip_converges(repo, tmp_path):
    """export -> import into a fresh store converges; body teleports."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _write(repo, "m.py", SRC)
    sha = _write(repo, "m.py", SRC.replace("x + 1", "x + 10"))
    capture_ops(sha, actor="a", repo_path=str(repo), data_root=src)

    bundle = export_delta(set(), data_root=src)
    assert bundle["op_count"] == 1
    first = import_delta(bundle, data_root=dst)
    second = import_delta(bundle, data_root=dst)
    assert first["imported_ops"] == 1
    assert second["imported_ops"] == 0  # idempotent — git pull, no conflicts
    assert sync_status(data_root=dst)["ops"] == 1
    store = BodyStore(data_root=dst)
    from awgit.oplog import OpLog

    op = OpLog(data_root=dst).all_ops()[0]
    alpha = next(nc for nc in op.node_changes if nc.symbol == "alpha")
    assert store.get(alpha.new_body_sha) is not None  # body teleported


# ---------------------------------------------------------------------------
# `awgit ledger` — the lookup must accept the ids the listing PRINTS
# ---------------------------------------------------------------------------
#
# Regression for a defect that shipped in 0.3.0 and then got REINTRODUCED one
# layer down while being fixed:
#
#   1. The listing printed `ledger_ref` as column 1 and the op_id nowhere, but
#      `--op` matched only `op_id`. So `awgit ledger --op <id-you-just-saw>`
#      answered "no ops match" — which reads as "that op does not exist", not
#      as "you passed the wrong one of two ids you were never shown".
#   2. The first fix printed the op_id too — abbreviated to 16 chars — while
#      still matching on EQUALITY. Same bug, new id: the lookup again rejected
#      the exact string it had printed.
#
# The invariant that actually closes it, and the only one worth testing:
# EVERY identifier appearing in the listing must round-trip through `--op`.

def _ledger_op(repo, tmp_path):
    """Capture one real commit into an isolated op-log and return (op, entry)."""
    from awgit.ledger import op_to_ledger_entry

    data = tmp_path / "awgit-data"
    # Two commits: capture diffs against a PARENT, so a repo's first commit
    # yields no op at all.
    _write(repo, "m.py", SRC)
    sha = _write(repo, "m.py", SRC.replace("x + 1", "x + 10"))
    op = capture_ops(sha, actor="test", repo_path=str(repo), data_root=data)
    assert op is not None, "capture produced no op"
    return op, op_to_ledger_entry(op)


def _matches(op, entry, token):
    """The matcher `awgit ledger --op` uses: PREFIX over op_id or ledger_ref."""
    return op.op_id.startswith(token) or (entry.ledger_ref or "").startswith(token)


def test_ledger_lookup_accepts_every_printed_identifier(repo, tmp_path):
    op, entry = _ledger_op(repo, tmp_path)

    # Every string a human can copy off a listing line, including the
    # abbreviated forms the listing actually renders.
    printed = {
        "ledger_ref": entry.ledger_ref,
        "op_id_short": op.op_id[:16],
        "op_id_full": op.op_id,
        "ledger_ref_short": entry.ledger_ref[:16],
    }
    for name, token in printed.items():
        assert token, f"listing printed an empty {name}"
        assert _matches(op, entry, token), (
            f"--op {token!r} ({name}) matched nothing, but the listing prints it"
        )


def test_ledger_lookup_rejects_a_foreign_id(repo, tmp_path):
    """Mutation guard: the prefix matcher must still be able to MISS, or the
    test above passes for the wrong reason."""
    op, entry = _ledger_op(repo, tmp_path)
    assert not _matches(op, entry, "zzzzzzzzzzzzzzzz")


def test_ledger_json_carries_the_full_op(repo, tmp_path):
    """The text form is a lossy human view. Anything programmatic — a
    world-model seeder, a reward program, an export — needs the op itself, not
    a re-parse of a display string that was never a contract."""
    op, _ = _ledger_op(repo, tmp_path)
    d = op.to_dict()
    for required in ("op_id", "git_sha", "node_changes", "actor_verified", "ledger_ref"):
        assert required in d, f"--json would omit {required}"
