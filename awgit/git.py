"""Passthrough to git, so ``awgit`` can be the only verb you type.

An agent told to "use awgit" then hits ``awgit log`` and gets "invalid choice",
learns that awgit is a sidecar, and goes back to git — at which point the
capture hooks are the only thing still participating. Forwarding the everyday
read verbs removes that cliff.

**No verb is repurposed. That rule is the whole design.** ``awgit diff`` has
meant a NODE-level diff since the package was written, and it is what the MCP
handler, both skills, the hooks and two blog posts call. Quietly re-pointing it
at ``git diff`` would change the RETURN SHAPE — node records become unified-diff
hunks — and the failure lands in a caller three steps away as a JSON key error,
long after the change. So the forwarded set is exactly the verbs awgit does NOT
already define, and git's own spelling of a taken verb is reachable through
``awgit git diff``.

``awgit commit`` is OWNED rather than forwarded: it is the one verb where awgit
has to do something first (check the lease) and after (capture the op).
"""

from __future__ import annotations

import subprocess
import sys
from typing import List, Sequence

#: Verbs forwarded to git untouched. Every one is absent from awgit's own
#: command set — see the module docstring for why that is a hard rule and not a
#: coincidence. Asserted by check_awgit_cli_contract ACC005.
PASSTHROUGH = (
    "add", "log", "show", "blame", "grep", "stash", "restore", "reset",
    "checkout", "switch", "branch", "rebase", "cherry-pick", "revert",
    "fetch", "remote", "tag", "describe", "shortlog", "reflog",
    # Added after a real dogfooding session: every verb reached for during an
    # end-to-end push test that was NOT here sent the user straight back to
    # `git`. A passthrough with holes is worse than none — it teaches people
    # the tool is unreliable, and they stop trying it.
    "ls-remote", "config", "rev-parse", "cat-file", "diff-tree", "ls-files",
    "apply", "mv", "rm", "clean", "notes", "archive", "gc", "fsck", "merge",
    # NOT "worktree": awgit owns that verb. The rewrite guard's refusal ends
    # in `awgit worktree new <name>`, so forwarding it to git would send the
    # reader of that message to a command with different arguments.
)


def run(args: Sequence[str], cwd: str = None) -> int:
    """Exec ``git <args>`` inheriting stdio, and return its exit code.

    stdio is INHERITED, not captured: git's pager, colour and progress output
    are what the caller expects, and buffering them through Python would break
    every interactive verb while looking fine in a test.
    """
    try:
        return subprocess.run(["git", *args], cwd=cwd).returncode
    except FileNotFoundError:
        print("awgit: git is not on PATH", file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        return 130


def forward(verb: str, rest: Sequence[str], cwd: str = None) -> int:
    """Forward one allowlisted verb plus its arguments."""
    return run([verb, *list(rest)], cwd=cwd)


def strip_separator(rest: Sequence[str]) -> List[str]:
    """Drop a single leading ``--`` from ``awgit git -- log --oneline``.

    argparse.REMAINDER keeps the separator, and passing it on makes git read
    every following token as a pathspec: ``git -- log`` is "no command, path
    named log", which fails with a message about ambiguous arguments that says
    nothing about the real cause.
    """
    out = list(rest)
    if out and out[0] == "--":
        out.pop(0)
    return out
