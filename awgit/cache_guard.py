"""Runner guard: look at git's derived caches before a read-only verb, via ``awgit.plugins``.

Registered on the ``GIT_RUN`` hook (see ``plugins.py``), so ``awgit log`` /
``awgit git ...`` pass through here and the generated modules are not forked.

Mode, env ``AWGIT_CACHE_GUARD`` (default ``warn``):

``off``   do nothing;
``warn``  before a read-only verb, run the ``quick`` cache check. If the
          multi-pack-index / commit-graph is bad, print ONE stderr line naming the
          file and the exact heal command, and run the verb unchanged. NEVER mutates;
``heal``  as ``warn``, but move the bad cache aside (``cache_integrity.heal``) and
          run the verb. Second line of defence, ``heal`` mode only: if an idempotent
          read verb FAILS with stderr matching ``improper chunk offset |
          multi-pack-index | commit-graph``, heal once and retry once.

Why the retry exists, honestly: the preflight reads the bytes on disk. On a 9p/NTFS
mount the page cache can serve stale pages for a file another process just
rewrote, so git can see a table of contents the preflight did not, and a clean
preflight proves little. git's own error is the only reliable signal, hence the
pattern-matched retry. Because that error means "git read something wrong", the
retry moves the implicated caches aside even when our bytes-on-disk check passes;
that costs speed, never data (they regenerate).

The quick check is O(1) (header + TOC + size) and is memoised on the files'
(mtime, size), so repeated calls in one process cost three ``stat`` calls.

Retry limits: the first attempt of a read verb is captured so a failed attempt is
not shown before the retry. stdout is captured only when it is not a terminal; on a
tty it streams (and a pager still works), so a verb that printed partial output on a
tty before failing may repeat it after the retry.

Separately (every mode but ``off``): a ``fetch``/``pull`` given ``--depth``,
``--deepen``, ``--shallow-since`` or ``--shallow-exclude`` in a repo that is not
shallow gets a one-line warning, because a shallow repo makes git silently ignore
the commit-graph. It warns and runs; ``AWGIT_ALLOW_SHALLOW=1`` silences it. It
cannot catch a tool that runs ``git fetch --depth`` itself, only fetches that go
through awgit.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from awgit import cache_integrity as ci
from awgit import plugins

ENV = "AWGIT_CACHE_GUARD"
MODES = ("off", "warn", "heal")
DEFAULT_MODE = "warn"

#: Verbs that only read (idempotent: safe to run twice, safe to check first).
READ_VERBS = frozenset({"log", "show", "cat-file", "rev-parse", "diff", "blame", "grep",
                        "ls-files", "status", "rev-list", "merge-base"})
RETRY_PATTERN = re.compile(r"improper chunk offset|multi-pack-index|commit-graph")
#: flags that make a fetch/pull shallow (see _warn_shallow)
SHALLOW_FLAGS = frozenset({"--depth", "--deepen", "--shallow-since", "--shallow-exclude"})
ALLOW_SHALLOW_ENV = "AWGIT_ALLOW_SHALLOW"

logger = logging.getLogger(__name__)

# git_dir -> (signature, bad verdicts). Per process; a stat signature, not a timer.
_memo: Dict[str, Tuple[tuple, List[Dict[str, Any]]]] = {}
_announced: set = set()


def mode(env: Optional[Dict[str, str]] = None) -> str:
    raw = (os.environ if env is None else env).get(ENV, "").strip().lower()
    return raw if raw in MODES else DEFAULT_MODE


def _signature(git_dir: Path) -> tuple:
    files, _extra = ci.commit_graph_paths(git_dir)
    chain = Path(git_dir) / "objects" / "info" / "commit-graphs" / "commit-graph-chain"
    sig = []
    for p in [ci.midx_path(git_dir), chain, *files]:
        try:
            st = os.stat(p)
            sig.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((str(p), None, None))
    return tuple(sig)


def preflight(git_dir: Path) -> List[Dict[str, Any]]:
    """Bad cache verdicts for ``git_dir`` (quick check), memoised on (mtime, size)."""
    key = str(git_dir)
    sig = _signature(git_dir)
    hit = _memo.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    bad = [v for v in ci.check_caches(git_dir) if v["state"] == ci.BAD]
    _memo[key] = (sig, bad)
    return bad


def forget(git_dir: Optional[Path] = None) -> None:
    """Drop memoised verdicts (after a heal, and in tests)."""
    if git_dir is None:
        _memo.clear()
        _announced.clear()
    else:
        _memo.pop(str(git_dir), None)


def _say(msg: str) -> None:
    print(f"awgit: cache guard: {msg}", file=sys.stderr)


def _exec(args: Sequence[str], cwd: Optional[str]) -> int:
    from awgit.git import _exec as plain

    return plain(args, cwd)


def _capture(args: Sequence[str], cwd: Optional[str]) -> Tuple[int, bytes, bytes, bool]:
    """Run git capturing stderr (and stdout unless it is a tty). (rc, out, err, out_captured)."""
    cap_out = not sys.stdout.isatty()
    try:
        p = subprocess.run(["git", *args], cwd=cwd, stderr=subprocess.PIPE,
                           stdout=subprocess.PIPE if cap_out else None)
    except FileNotFoundError:
        return 127, b"", b"awgit: git is not on PATH\n", True
    return p.returncode, p.stdout or b"", p.stderr or b"", cap_out


def _replay(out: bytes, err: bytes, out_captured: bool) -> None:
    # git already ran: a closed pipe here (`awgit log | head`) must not escape,
    # or the hook seam would read the exception as "not handled" and run git again.
    try:
        if out_captured and out:
            sys.stdout.flush()
            sys.stdout.buffer.write(out)
            sys.stdout.buffer.flush()
        if err:
            sys.stderr.flush()
            sys.stderr.buffer.write(err)
            sys.stderr.buffer.flush()
    except (OSError, ValueError):
        pass


def _kinds_from(stderr: str) -> List[str]:
    """Which caches git's message implicates. 'improper chunk offset' is printed by
    BOTH chunk-format readers, so unattributed it implicates both."""
    kinds = []
    if "multi-pack-index" in stderr:
        kinds.append(ci.MIDX)
    if "commit-graph" in stderr:
        kinds.append(ci.COMMIT_GRAPH)
    return kinds or [ci.MIDX, ci.COMMIT_GRAPH]


def shallow_flags(args: Sequence[str]) -> List[str]:
    """The depth-limiting flags in a ``fetch``/``pull`` argv (``--depth=1`` and
    ``--depth 1`` both). ``--unshallow`` is the cure and is never one of them."""
    return [a for a in args[1:] if a.split("=", 1)[0] in SHALLOW_FLAGS]


def _warn_shallow(args: Sequence[str], cwd: Optional[str]) -> None:
    """Warn (never block, never mutate) before a fetch/pull would SHALLOW a full repo.

    ``git fetch --depth=N`` in the shared checkout writes ``.git/shallow``, after
    which git silently ignores the commit-graph and merge-base/ahead-behind are
    wrong. Only warns when the repo is not shallow yet; ``AWGIT_ALLOW_SHALLOW=1``
    silences it for the person who means it.

    Limit, stated plainly: this sees only fetches that go THROUGH awgit. A tool that
    runs ``git fetch --depth`` itself (release_portal_kit_to_products.py,
    check_appliance_release_current.py ...) never reaches this code.
    """
    if os.environ.get(ALLOW_SHALLOW_ENV, "").strip() in ("1", "true", "yes"):
        return
    flags = shallow_flags(args)
    if not flags:
        return
    gd = ci.resolve_git_dir(cwd) if cwd else ci.resolve_git_dir()
    if gd is None or ci.shallow_state(gd).get("shallow") is not False:
        return
    _say(f"`git {args[0]} {' '.join(flags)}` will make {gd} SHALLOW: git then ignores the commit-graph "
         f"and merge-base/ahead-behind go wrong. To undo: git fetch --unshallow origin <branch>. "
         f"Silence with {ALLOW_SHALLOW_ENV}=1. Continuing unchanged.")


def guard_run(args: Sequence[str], cwd: Optional[str], run_plain: Any = None) -> int:
    """The ``GIT_RUN`` hook: returns git's exit code. Never raises before git ran."""
    plain = run_plain or _exec
    m = mode()
    verb = args[0] if args else ""
    if m != "off" and verb in ("fetch", "pull"):
        try:
            _warn_shallow(args, cwd)
        except Exception as exc:  # noqa: BLE001 - advisory only
            logger.debug("shallow warning skipped: %s", exc)
    if m == "off" or verb not in READ_VERBS:
        return plain(args, cwd)
    gd: Optional[Path] = None
    try:
        gd = ci.resolve_git_dir(cwd) if cwd else ci.resolve_git_dir()
        bad = preflight(gd) if gd is not None else []
    except Exception as exc:  # noqa: BLE001 - a guard that cannot look must not stop git
        _say(f"preflight could not run ({type(exc).__name__}: {exc}); running {verb} unguarded")
        return plain(args, cwd)
    if bad and gd is not None:
        try:
            if m == "warn":
                key = (str(gd), tuple(v["path"] for v in bad))
                if key not in _announced:
                    _announced.add(key)
                    _say(f"{bad[0]['path']} is bad ({bad[0]['why']}); git may report 'improper chunk "
                         f"offset'. Heal: awgit caches --repo {gd} --fix  "
                         f"(or set {ENV}=heal). Continuing unchanged.")
            else:
                res = ci.heal(gd, trigger="guard-preflight")
                forget(gd)
                if res["refused"]:
                    _say(f"not healed: {res['refused']}. Continuing unchanged.")
                else:
                    for mv in res["moved"]:
                        _say(f"moved bad cache aside: {mv['from']} -> {mv.get('to', 'FAILED')}")
        except Exception as exc:  # noqa: BLE001
            _say(f"could not act on {bad[0]['path']} ({type(exc).__name__}: {exc}); continuing")
    if m != "heal" or gd is None:
        return plain(args, cwd)
    # heal mode second line of defence: retry once on git's own cache error
    rc, out, err, out_cap = _capture(args, cwd)
    text = err.decode("utf-8", "replace")
    if rc == 0 or not RETRY_PATTERN.search(text):
        _replay(out, err, out_cap)
        return rc
    try:
        res = ci.heal(gd, force=_kinds_from(text), trigger="guard-retry")
        forget(gd)
    except Exception as exc:  # noqa: BLE001
        res = {"refused": f"{type(exc).__name__}: {exc}", "moved": []}
    if res["refused"] or not res["moved"]:
        _replay(out, err, out_cap)
        _say(f"git failed with a cache error and it could not be healed "
             f"({res['refused'] or 'nothing to move aside'})")
        return rc
    _say(f"git reported a cache error; moved {len(res['moved'])} cache file(s) aside, retrying {verb} once")
    return plain(args, cwd)


def install() -> bool:
    """Register the guard on ``GIT_RUN`` unless a host already registered one."""
    if plugins.registered(plugins.GIT_RUN):
        return False
    plugins.register(plugins.GIT_RUN, guard_run)
    return True
