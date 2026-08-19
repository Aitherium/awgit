"""The lease-request sidecar: it must never be able to block a commit.

This ships in a published package and its whole purpose is unblocking people, so
the property that matters most is the defensive one -- a damaged, missing or
hostile sidecar degrades to empty rather than raising into the caller. Every
call site wraps it, but a store that throws is one refactor away from breaking
`awgit lease list` for everyone.

The other assertions pin the three decisions that are easy to regress:
idempotence (a retry loop must not spam the holder into ignoring the mechanism),
the TTL (a request must not outlive the lease it is about), and the cap (a
runaway loop must not fill the disk Postgres shares -- that has taken this box
down before).
"""

import json
import time

import pytest

from awgit.lease_requests import (
    MAX_REQUESTS,
    REQUEST_TTL_SEC,
    LeaseRequests,
    format_pending,
)


@pytest.fixture()
def store(tmp_path):
    return LeaseRequests(tmp_path)


def test_add_then_read_back_by_holder(store):
    store.add("a/b.py", to_actor="claude:holder", from_actor="claude:me",
              message="need it")
    got = store.for_actor("claude:holder")
    assert len(got) == 1
    assert got[0]["target"] == "a/b.py"
    assert got[0]["from_actor"] == "claude:me"
    assert got[0]["message"] == "need it"
    # and the asker can see what they are waiting on
    assert len(store.by_actor("claude:me")) == 1


def test_requests_are_directional(store):
    """A request to X must not show up as a request to Y."""
    store.add("a/b.py", to_actor="claude:x", from_actor="claude:me")
    assert store.for_actor("claude:y") == []


def test_reasking_updates_in_place_rather_than_stacking(store):
    """Spam is how a mechanism like this gets ignored."""
    for _ in range(5):
        store.add("a/b.py", to_actor="claude:holder", from_actor="claude:me",
                  message="please")
    got = store.for_actor("claude:holder")
    assert len(got) == 1, "re-asking must not stack duplicates"
    assert got[0]["count"] == 5, "but the holder should see it was asked 5x"


def test_distinct_askers_are_separate_requests(store):
    store.add("a/b.py", to_actor="claude:holder", from_actor="claude:one")
    store.add("a/b.py", to_actor="claude:holder", from_actor="claude:two")
    assert len(store.for_actor("claude:holder")) == 2


def test_expired_requests_are_dropped_on_read(store, tmp_path):
    """A request must not outlive the lease it is about."""
    store.add("a/b.py", to_actor="claude:holder", from_actor="claude:me")
    raw = json.loads((tmp_path / "lease_requests.json").read_text())
    raw["requests"][0]["ts"] = time.time() - (REQUEST_TTL_SEC + 60)
    (tmp_path / "lease_requests.json").write_text(json.dumps(raw))
    assert store.for_actor("claude:holder") == []


def test_store_is_capped(store):
    """A runaway loop must not fill the disk Postgres shares."""
    for i in range(MAX_REQUESTS + 25):
        store.add(f"f{i}.py", to_actor="claude:holder", from_actor="claude:me")
    assert len(store.for_actor("claude:holder")) <= MAX_REQUESTS


def test_clear_is_scoped_to_the_holder(store):
    store.add("a.py", to_actor="claude:h1", from_actor="claude:me")
    store.add("b.py", to_actor="claude:h2", from_actor="claude:me")
    assert store.clear("claude:h1") == 1
    assert store.for_actor("claude:h1") == []
    assert len(store.for_actor("claude:h2")) == 1, "must not clear someone else"


def test_clear_can_target_specific_paths(store):
    store.add("a.py", to_actor="claude:h", from_actor="claude:me")
    store.add("b.py", to_actor="claude:h", from_actor="claude:me")
    assert store.clear("claude:h", targets=["a.py"]) == 1
    assert [r["target"] for r in store.for_actor("claude:h")] == ["b.py"]


# ── the defensive properties: this must NEVER raise into a caller ──────────

def test_corrupt_sidecar_degrades_to_empty(tmp_path):
    """A damaged store must not break `awgit lease list` for everyone."""
    (tmp_path / "lease_requests.json").write_text("{not json at all")
    s = LeaseRequests(tmp_path)
    assert s.for_actor("claude:anyone") == []
    s.add("a.py", to_actor="claude:h", from_actor="claude:me")   # still writes
    assert len(s.for_actor("claude:h")) == 1


def test_wrong_shape_degrades_to_empty(tmp_path):
    """`requests` present but not a list -- a hand-edit, or a bad migration."""
    (tmp_path / "lease_requests.json").write_text('{"requests": {"a": 1}}')
    assert LeaseRequests(tmp_path).for_actor("claude:h") == []


def test_non_dict_entries_are_skipped_not_crashed(tmp_path):
    (tmp_path / "lease_requests.json").write_text(
        '{"requests": ["a string", null, 7]}')
    assert LeaseRequests(tmp_path).for_actor("claude:h") == []


def test_missing_file_is_not_an_error(tmp_path):
    assert LeaseRequests(tmp_path / "nope").for_actor("claude:h") == []


def test_unwritable_location_does_not_raise(tmp_path):
    """Saving is best-effort: it warns, it does not take the caller down."""
    blocked = tmp_path / "afile"
    blocked.write_text("x")                    # a FILE where a dir is needed
    s = LeaseRequests(blocked / "sub")
    s.add("a.py", to_actor="claude:h", from_actor="claude:me")  # must not raise


# ── the message a human or an agent actually reads ────────────────────────

def test_format_pending_is_empty_when_nothing_is_pending():
    assert format_pending([]) == ""


def test_format_pending_names_the_command_to_run():
    out = format_pending([{"target": "a/b.py", "from_actor": "claude:abcdef12",
                           "message": "need it", "ts": time.time(),
                           "count": 1}])
    assert "a/b.py" in out
    assert "abcdef12" in out
    assert "need it" in out
    # A notification that leaves the reader to work out the command is how this
    # ends up ignored.
    assert "awgit lease release" in out


def test_format_pending_truncates_rather_than_flooding():
    many = [{"target": f"f{i}.py", "from_actor": "claude:x", "message": "",
             "ts": time.time(), "count": 1} for i in range(40)]
    out = format_pending(many, limit=5)
    assert "and 35 more" in out
