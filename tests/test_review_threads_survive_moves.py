"""A review thread must still point at the function after the code moves.

A GitHub comment is anchored to (commit, file, line). Rebase and the line moves;
reformat and it moves further; move the function to another file and the anchor
is gone — the thread goes "outdated", collapses, and the objection it recorded
stops being in front of anyone. Nothing errors. The review just quietly loses an
argument someone made.

garden fixes half of it by anchoring to a COMMIT, so a rebase does not orphan
the thread. The test that matters here is the other half: the MOVE. awgit
anchors to a node id, so the thread re-resolves to wherever the function now
lives, and the line number is computed rather than stored.

The deletion case is asserted too, in the other direction: a thread whose node
is really gone must be shown as ORPHANED, not dropped. "The function you
objected to no longer exists" is a review outcome, not a reason to hide the
objection.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit import review  # noqa: E402
from awgit.bridge import install_hooks  # noqa: E402
from awgit.capture import _node_records, load_node_manager  # noqa: E402
from awgit.data_root import vcs_data_root  # noqa: E402

ALPHA = "def alpha():\n    return 1\n"
BETA = "def beta():\n    return 2\n"


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
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "T")
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "vcs"))
    monkeypatch.setenv("VCS_LEASES_ENFORCE", "0")
    install_hooks(str(r))
    monkeypatch.chdir(r)
    # A seed commit FIRST, because capture_ops returns None for a ROOT commit
    # ("nothing to record" — a first commit adds everything, which is noise).
    # Without a parent nothing is captured, nothing is registered, and every
    # id_for() call mints a fresh EPHEMERAL id — so two callers asking for the
    # same function's id get different answers and every thread reads as
    # orphaned. Node identity is a thing capture creates, not a thing the
    # parser computes.
    (r / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(r, "add", "seed.txt")
    git(r, "commit", "-q", "-m", "seed")
    return r


def commit_all(repo: Path, message: str) -> None:
    """Commit, which CAPTURES — and capture is what registers node ids.

    `load_node_manager` is deliberately read-only: an unregistered symbol gets
    an EPHEMERAL id for that call, so two callers computing an id for the same
    function get different answers and every thread looks orphaned. Node
    identity exists because capture persisted it, which is exactly why the
    workflow is "comment on a node the diff showed you".
    """
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def node_id_of(repo: Path, rel: str, symbol: str) -> str:
    """The stable id of one symbol, read from the REGISTERED universe."""
    manager = load_node_manager(vcs_data_root())
    for record in _node_records((repo / rel).read_bytes(), rel):
        if record["name"] == symbol:
            return manager.id_for(record["name"], record["path"], record["type"])
    raise AssertionError(f"{symbol} not found in {rel}")


def test_a_thread_resolves_to_the_current_line(repo: Path):
    (repo / "m.py").write_text(ALPHA + "\n" + BETA, encoding="utf-8")
    commit_all(repo, "add alpha and beta")
    nid = node_id_of(repo, "m.py", "beta")
    review.add_comment("Ichange1", nid, "bounds?", "reviewer",
                       symbol="beta", path="m.py")

    where = review.locate(nid, "m.py", repo)
    assert where is not None, "the node must be found where it is"
    assert where[0] == "m.py"
    assert where[1] == 4, f"beta starts at line 4, got {where[1]}"


def test_the_line_is_computed_not_stored(repo: Path):
    """Insert code ABOVE the node. A stored line would now point at the wrong
    function while looking perfectly valid."""
    (repo / "m.py").write_text(ALPHA + "\n" + BETA, encoding="utf-8")
    commit_all(repo, "add alpha and beta")
    nid = node_id_of(repo, "m.py", "beta")
    review.add_comment("Ichange1", nid, "bounds?", "reviewer",
                       symbol="beta", path="m.py")
    before = review.locate(nid, "m.py", repo)

    (repo / "m.py").write_text("import os\nimport sys\n\n" + ALPHA + "\n" + BETA,
                               encoding="utf-8")
    after = review.locate(nid, "m.py", repo)
    assert after is not None and after[1] == before[1] + 3, (
        f"the thread must follow the node down the file: {before} -> {after}"
    )


def test_a_thread_survives_the_function_moving_to_another_file(repo: Path):
    """THE claim. A commit-anchored comment survives a rebase and not this."""
    (repo / "m.py").write_text(ALPHA + "\n" + BETA, encoding="utf-8")
    commit_all(repo, "add alpha and beta")
    nid = node_id_of(repo, "m.py", "beta")
    review.add_comment("Ichange1", nid, "bounds?", "reviewer",
                       symbol="beta", path="m.py")

    # beta moves out to its own file; the hint path no longer contains it.
    (repo / "m.py").write_text(ALPHA, encoding="utf-8")
    (repo / "other.py").write_text("# moved\n" + BETA, encoding="utf-8")

    where = review.locate(nid, "m.py", repo, search=["other.py"])
    assert where is not None, (
        "the thread was orphaned by a MOVE — this is precisely what anchoring "
        "to a node id is supposed to survive"
    )
    assert where[0] == "other.py"
    assert where[1] == 2


def test_a_deleted_node_is_orphaned_visibly_not_dropped(repo: Path):
    (repo / "m.py").write_text(ALPHA + "\n" + BETA, encoding="utf-8")
    commit_all(repo, "add alpha and beta")
    nid = node_id_of(repo, "m.py", "beta")
    review.add_comment("Ichange1", nid, "bounds?", "reviewer",
                       symbol="beta", path="m.py")

    (repo / "m.py").write_text(ALPHA, encoding="utf-8")   # beta is gone
    assert review.locate(nid, "m.py", repo) is None

    rendered = "\n".join(review.render(review.load("Ichange1"), repo))
    assert "orphaned" in rendered, rendered
    assert "bounds?" in rendered, (
        "the comment text must still be shown — a lost objection is worse than "
        "a stale one"
    )


def test_comments_are_drafts_until_submit(repo: Path):
    (repo / "m.py").write_text(ALPHA, encoding="utf-8")
    commit_all(repo, "add alpha")
    nid = node_id_of(repo, "m.py", "alpha")
    review.add_comment("Ichange2", nid, "first", "reviewer", path="m.py")
    review.add_comment("Ichange2", nid, "second", "reviewer", path="m.py")

    threads = review.load("Ichange2")
    assert len(threads) == 1, "two comments on one node are ONE thread"
    assert all(c.draft for c in threads[0].comments)

    published = review.submit("Ichange2")
    assert len(published) == 2
    assert not any(c.draft for c in review.load("Ichange2")[0].comments)


def test_open_threads_are_what_blocks_a_merge(repo: Path):
    (repo / "m.py").write_text(ALPHA, encoding="utf-8")
    commit_all(repo, "add alpha")
    nid = node_id_of(repo, "m.py", "alpha")
    thread = review.add_comment("Ichange3", nid, "no", "reviewer", path="m.py")
    assert len(review.unresolved("Ichange3")) == 1

    assert review.resolve("Ichange3", thread.thread_id[:8])
    assert review.unresolved("Ichange3") == []


def test_the_store_survives_a_reload(repo: Path):
    (repo / "m.py").write_text(ALPHA, encoding="utf-8")
    commit_all(repo, "add alpha")
    nid = node_id_of(repo, "m.py", "alpha")
    review.add_comment("Ichange4", nid, "persisted?", "reviewer", path="m.py")
    again = review.load("Ichange4")
    assert again and again[0].comments[0].body == "persisted?"


def test_an_ambiguous_move_is_refused_rather_than_guessed(repo: Path):
    """Two same-named functions: the wrong one must NOT inherit the objection.

    The cross-file fallback matches on (name, type) because the node id is
    keyed on (name, path) and capture's rename detection is scoped to one file.
    That fallback is only sound while it refuses ambiguity — silently attaching
    a reviewer's objection to a different function is worse than showing the
    thread as orphaned, because it reads as answered.
    """
    (repo / "m.py").write_text(ALPHA + "\n" + BETA, encoding="utf-8")
    commit_all(repo, "add alpha and beta")
    nid = node_id_of(repo, "m.py", "beta")

    # beta vanishes from m.py and TWO candidates now define a `beta`.
    (repo / "m.py").write_text(ALPHA, encoding="utf-8")
    (repo / "one.py").write_text(BETA, encoding="utf-8")
    (repo / "two.py").write_text(BETA, encoding="utf-8")

    assert review.locate(nid, "m.py", repo, search=["one.py"]) is not None, (
        "a single candidate must still resolve")
    assert review.locate(nid, "m.py", repo, search=["one.py", "two.py"]) is None, (
        "two candidates named `beta` are ambiguous — refuse, do not pick one")
