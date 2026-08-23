"""The lease gate must survive a busy tree.

`_ls_index` passed every path to one `git ls-files` call. Windows' CreateProcess
rejects a command line over ~32KB with WinError 206 ("The filename or extension
is too long"), and the pre-commit lease gate calls this with every staged path in
the repo.

Measured 2026-08-19 on a shared worktree carrying ~2243 staged/untracked paths:
the gate raised before it could judge anything, so EVERY commit in the repo was
refused -- by a traceback, not by a lease decision. The error named `subprocess`
and `CreateProcess` and no path at all, so it read as a broken toolchain rather
than "your argument list is too long".

The shape is the nasty part: it appears only under load and vanishes on a clean
checkout, so it is worst exactly when the tree is busiest and hardest to
reproduce afterwards.

These tests assert the CHUNKING rather than the outcome, because a test that just
calls the function on a small repo passes on the broken version too.
"""
from __future__ import annotations

import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

from awgit import cli  # noqa: E402


def test_a_large_path_list_is_split_into_several_calls(monkeypatch, tmp_path):
    """The regression: one call carrying thousands of paths."""
    calls: list[list[str]] = []

    class _Result:
        stdout = ""

    def fake_run(argv, **_kw):
        calls.append(argv)
        # Argv length is the thing that actually broke. Assert it here so the
        # test fails for the REAL reason rather than on a call count someone
        # could satisfy by chunking at 10000.
        assert sum(len(a) + 1 for a in argv) < 30000, (
            "argv exceeds the Windows CreateProcess limit -- this is WinError 206"
        )
        return _Result()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    paths = [f"AitherOS/some/deep/path/module_{i:05d}.py" for i in range(2243)]
    cli._ls_index(tmp_path, str(tmp_path / "idx"), paths)

    assert len(calls) > 1, "a 2243-path list was passed in a single call"
    assert sum(len(c) - 4 for c in calls) == len(paths), (
        "chunking dropped or duplicated paths -- every path must be asked about "
        "exactly once, or the gate silently stops judging some of them"
    )


def test_an_empty_list_makes_no_call(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: called.append(a))
    assert cli._ls_index(tmp_path, str(tmp_path / "idx"), []) == {}
    assert not called


def test_a_small_list_is_still_one_call(monkeypatch, tmp_path):
    """Chunking must not turn every ordinary commit into many subprocesses."""
    calls = []

    class _Result:
        stdout = ""

    monkeypatch.setattr(cli.subprocess, "run",
                        lambda argv, **k: (calls.append(argv), _Result())[1])
    cli._ls_index(tmp_path, str(tmp_path / "idx"), ["a.py", "b.py"])
    assert len(calls) == 1


def test_results_from_every_chunk_are_merged(monkeypatch, tmp_path):
    """A chunk whose output is dropped is a path the gate stopped judging."""
    class _Result:
        def __init__(self, out):
            self.stdout = out

    seen = {"n": 0}

    def fake_run(argv, **_kw):
        seen["n"] += 1
        # one fabricated blob line per call, keyed by call number
        return _Result(f"100644 blob{seen['n']} 0\tfile_{seen['n']}.py")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    paths = [f"f{i}.py" for i in range(cli._LS_INDEX_CHUNK * 2 + 5)]
    blobs = cli._ls_index(tmp_path, str(tmp_path / "idx"), paths)
    assert len(blobs) == seen["n"] >= 3, (
        "results from later chunks were discarded -- the gate would judge only "
        "the first chunk and report the rest as absent"
    )
