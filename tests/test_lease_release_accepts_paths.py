"""`awgit lease release` must never report success for having done nothing.

`acquire` takes PATHS and `release` takes lease IDS, so passing the same string
to both is the natural mistake. It used to be a SILENT one: an argument matching
no lease id released nothing, printed `vcs: released 0 leases`, and exited **0**
-- while `awgit lease list` still showed the lease held. Measured 2026-08-16, it
cost a session: the release "succeeded", the lease stayed, and the next edit was
blocked by the caller's OWN lease with a message saying another session held it.

That is the silent-no-op class in `.claude/rules/security-review-patterns.md` #5
-- a fail-closed path that always returns empty passes every "returns nothing"
assertion trivially. The rule is therefore asserted from both directions: the
positive one (a path really does release the lease) and the negative one (an
argument that resolves to nothing EXITS NON-ZERO rather than counting zero).

Each test carries a mutation guard describing the old shape, so a future
refactor that reintroduces `registry.release(who, args.ids)` fails here.
"""
from __future__ import annotations

import shutil
import subprocess
import uuid

import pytest


# The console script, not `python -m awgit`: the package ships no __main__.py,
# so `-m` dies with "'awgit' is a package and cannot be directly executed" --
# failing these tests for a reason unrelated to what they assert.
#
# Resolved and gated at COLLECTION time, never with pytest.skip() in a body:
# a body-level skip fires after partial execution, so a real failure is reported
# as "skipped" and CI stays green.
_AWGIT = shutil.which("awgit")
pytestmark = pytest.mark.skipif(
    _AWGIT is None, reason="awgit console script not on PATH")


def _awgit(*args: str, actor: str) -> subprocess.CompletedProcess:
    # `lease list` accepts no --actor (it prints every active lease), and
    # passing one is an argparse ERROR, not an ignored flag -- so a helper that
    # appends it unconditionally makes `list` return empty stdout and every
    # assertion against the listing fails for the wrong reason.
    argv = [_AWGIT, "lease", *args]
    if args and args[0] != "list":
        argv += ["--actor", actor]
    # encoding= is required, not optional: text=True alone decodes with
    # the LOCALE codec -- cp1252 on this host -- and awgit's own output carries
    # non-ASCII, so a UnicodeDecodeError would surface as a ValueError that no
    # OSError/SubprocessError guard catches. The test would then crash rather
    # than report a verdict.
    return subprocess.run(
        argv, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


@pytest.fixture()
def actor() -> str:
    # Unique per run: the registry is process-global shared state, and a fixed
    # actor would make these tests order-dependent against any concurrent
    # session holding leases (the class gate 1k exists for).
    return f"test-lease-release-{uuid.uuid4().hex[:8]}"


def _target(actor: str) -> str:
    return f"AitherOS/dev/tools/_probe_{actor}.txt"


def test_release_by_path_actually_releases(actor: str) -> None:
    """The positive assertion. Without it, a release that always returns 0
    passes any test that only checks 'the lease is not held afterwards'."""
    path = _target(actor)
    assert _awgit("acquire", path, actor=actor).returncode == 0

    rel = _awgit("release", path, actor=actor)
    assert rel.returncode == 0, rel.stderr
    # MUTATION GUARD: the old code called registry.release(who, args.ids) with
    # the raw path, which matched no id -> "released 0 leases", exit 0.
    assert "released 0 leases" not in rel.stdout, (
        "release reported zero for a path it should have resolved -- the "
        "silent no-op is back"
    )
    assert "released 1 leases" in rel.stdout

    listing = _awgit("list", actor=actor)
    assert path not in listing.stdout, "lease survived a successful release"


def test_release_by_id_still_works(actor: str) -> None:
    """Regression guard: resolving paths must not break the documented form."""
    path = _target(actor)
    out = _awgit("acquire", path, actor=actor).stdout + _awgit(
        "list", actor=actor).stdout
    lease_id = next(
        (tok for line in out.splitlines() for tok in line.split()
         if len(tok) == 32 and all(c in "0123456789abcdef" for c in tok)),
        None,
    )
    assert lease_id, f"could not find a lease id in: {out!r}"

    rel = _awgit("release", lease_id, actor=actor)
    assert rel.returncode == 0, rel.stderr
    assert "released 1 leases" in rel.stdout


def test_unresolvable_argument_exits_nonzero(actor: str) -> None:
    """The half that makes this a gate rather than a convenience.

    A count of zero is not a verdict. An argument naming neither a lease id nor
    a leased path is a caller error and must be loud -- otherwise every typo
    reads as 'there was nothing to release', which is exactly how the original
    defect hid.
    """
    rel = _awgit("release", "definitely-not-a-lease", actor=actor)
    assert rel.returncode != 0, (
        "an unresolvable argument exited 0 -- callers cannot distinguish "
        "'released nothing' from 'released everything you asked for'"
    )
    assert "no active lease of yours matches" in rel.stderr


def test_release_does_not_touch_another_actors_lease(actor: str) -> None:
    """Path resolution is scoped to the caller.

    Resolving a path against ALL active leases would let one session release a
    peer's lease by naming a file -- turning a usability fix into a way to
    defeat the concurrency guard the registry exists to provide.
    """
    peer = f"{actor}-peer"
    path = _target(actor)
    assert _awgit("acquire", path, actor=peer).returncode == 0
    try:
        rel = _awgit("release", path, actor=actor)
        assert rel.returncode != 0, (
            "released a path leased by a DIFFERENT actor -- path resolution "
            "must be scoped to the caller's own leases"
        )
        assert path in _awgit("list", actor=peer).stdout
    finally:
        _awgit("release", path, actor=peer)
