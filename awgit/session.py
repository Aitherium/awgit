"""Sessions — one agent window, one worktree, one branch off the CURRENT trunk.

The defect this closes, measured on this repo 2026-09-05: every concurrent
Claude Code window worked in ONE shared checkout on ONE long-lived branch
(``feat/tunnel-phone-coding``: 512 commits ahead of trunk, 282 behind, 1010
uncommitted files from seven live peers). Three consequences, and not one of
them announces itself:

* **Nothing starts from trunk.** ``awgit worktree new`` defaulted to ``--at
  HEAD``, so a "new" worktree inherited whatever the shared tree happened to
  be sitting on. A branch cut that way is born behind trunk and stays behind.
* **Nothing is isolated.** A rewrite, a checkout, or a reset in the shared
  tree lands on six other windows' uncommitted work. That is not a risk here;
  it is the recorded cause of a 1,774-line revert.
* **Nobody can tell the windows apart.** Every terminal shows the same branch
  and the same dirty tree, so "what is that window doing?" has no answer
  anywhere on the machine.

The registry is **one file per session**, never one shared file. Seven writers
against a single JSON is a lost-update race, and the losing write is silent —
the same reason the decision-card mailbox is per-message (DC004). Git stays
ground truth for what exists: a row whose worktree ``git worktree list`` does
not know about is reported STALE rather than believed, because a registry that
can disagree with git is worse than no registry.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: Branch kinds. Conventional-commit types, so the branch name and the commits
#: on it agree about what the work is.
KINDS = ("feat", "fix", "chore", "docs", "perf", "refactor", "test", "build", "ci")

#: A session with no heartbeat for this long is reported STALE. Long enough
#: that a window sitting idle mid-task is not evicted, short enough that a
#: closed terminal stops claiming a branch by the end of the day.
STALE_AFTER_S = 6 * 3600


def _git(repo: Optional[Path], *args: str) -> subprocess.CompletedProcess:
    """Run git, and treat an unusable cwd as a FAILED run, never an exception.

    A vanished worktree is the ordinary case here, not an edge one: git's own
    ``worktree remove`` unregisters before it deletes, so a registry row can
    outlive its directory by design. ``subprocess.run`` raises
    ``NotADirectoryError`` (WinError 267) for that cwd rather than returning a
    non-zero result, so every caller that reasonably expects a return code
    would get a traceback instead — found by this module's own self-test.
    """
    try:
        return subprocess.run(
            ["git", *args], cwd=str(repo) if repo else None, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=128, stdout="", stderr=str(exc))


def _ok(repo: Optional[Path], *args: str) -> str:
    proc = _git(repo, *args)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def registry_dir() -> Path:
    """Where session rows live. One file per session — see the module docstring."""
    from awgit.data_root import vcs_data_root

    path = vcs_data_root() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_id(worktree: Path) -> str:
    """Stable id for a worktree path — survives a terminal restart in place.

    Keyed on the PATH rather than a pid or a random token: the question a
    second window asks is "who owns this checkout", and a pid cannot answer
    that after the process holding it exits.
    """
    resolved = str(Path(worktree).resolve()).replace(chr(92), "/").lower()
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


def slugify(text: str) -> str:
    """A branch-safe slug. Empty in, empty out — the caller decides if that is fatal."""
    out: List[str] = []
    for ch in str(text or "").strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in " _-/." and out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")[:48]


def current_worktree(cwd: Optional[Path] = None) -> Optional[Path]:
    """This checkout's own root (the LINKED worktree, not the main one)."""
    top = _ok(cwd, "rev-parse", "--path-format=absolute", "--show-toplevel")
    return Path(top) if top else None


def _path_for(sid: str) -> Path:
    return registry_dir() / (sid + ".json")


def _write(row: Dict[str, object]) -> None:
    """Atomic per-session write. tmp+replace so a reader never sees half a row."""
    target = _path_for(str(row["id"]))
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(row, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)


def _read(tree: Path) -> Optional[Dict[str, object]]:
    path = _path_for(session_id(tree))
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # An unparseable row is not a session. Reporting it as one would let a
        # truncated write masquerade as a live claim on a branch.
        return None


def _row_for(tree: Path) -> Dict[str, object]:
    branch = _ok(tree, "rev-parse", "--abbrev-ref", "HEAD") or "(detached)"
    return {
        "id": session_id(tree),
        "worktree": str(tree),
        "branch": branch,
        "doing": "",
        "kind": branch.split("/", 1)[0] if "/" in branch else "",
        "started": time.time(),
        "heartbeat": time.time(),
        "pid": os.getpid(),
        "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "",
    }


def register(cwd: Optional[Path] = None, doing: str = "") -> Tuple[bool, str]:
    """Record this window in the registry, creating or refreshing its row."""
    tree = current_worktree(cwd)
    if tree is None:
        return False, "not inside a git worktree"
    row = _read(tree) or _row_for(tree)
    row["branch"] = _ok(tree, "rev-parse", "--abbrev-ref", "HEAD") or "(detached)"
    row["heartbeat"] = time.time()
    row["pid"] = os.getpid()
    if doing:
        row["doing"] = str(doing).strip()[:200]
    _write(row)
    return True, "session " + str(row["id"]) + " on " + str(row["branch"])


def describe(text: str, cwd: Optional[Path] = None) -> Tuple[bool, str]:
    """Set what THIS window is doing. Creates the row if the session is new."""
    tree = current_worktree(cwd)
    if tree is None:
        return False, "not inside a git worktree"
    row = _read(tree) or _row_for(tree)
    row["doing"] = str(text or "").strip()[:200]
    row["heartbeat"] = time.time()
    _write(row)
    return True, "session " + str(row["id"]) + ": " + (str(row["doing"]) or "(cleared)")


def end(cwd: Optional[Path] = None) -> Tuple[bool, str]:
    """Deregister this window. The worktree and the branch are left alone."""
    tree = current_worktree(cwd)
    if tree is None:
        return False, "not inside a git worktree"
    path = _path_for(session_id(tree))
    if not path.exists():
        return True, "not registered — nothing to end"
    path.unlink()
    return True, "ended session for " + str(tree)


def _worktrees(cwd: Optional[Path] = None) -> List[Tuple[str, str, str]]:
    from awgit import worktree as wt

    return wt.listing(cwd)


def listing(cwd: Optional[Path] = None) -> List[Dict[str, object]]:
    """Every registered session, joined against git's own worktree list.

    ``stale_tree`` is git's answer, not ours: a row git cannot see means the
    worktree was removed and the registry has not caught up.
    """
    known = {str(Path(p).resolve()).lower() for p, _, _ in _worktrees(cwd)}
    rows: List[Dict[str, object]] = []
    now = time.time()
    for path in sorted(registry_dir().glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tree = str(Path(str(row.get("worktree", ""))).resolve()).lower()
        row["stale_tree"] = tree not in known
        row["idle_s"] = int(now - float(row.get("heartbeat", 0) or 0))
        row["stale"] = bool(row["stale_tree"]) or int(row["idle_s"]) > STALE_AFTER_S
        rows.append(row)
    rows.sort(key=lambda r: float(r.get("heartbeat", 0) or 0), reverse=True)
    return rows


def start(slug: str, kind: str = "feat", base: str = "",
          cwd: Optional[Path] = None, fetch: bool = True,
          doing: str = "") -> Tuple[int, str, Optional[Path]]:
    """Cut a fresh worktree + branch from the CURRENT trunk.

    Returns ``(exit_code, message, path)``. Exit **2** — never 0 — when the
    trunk cannot be resolved: branching from an unknown base is exactly the
    silent-wrong-answer this exists to prevent, and a fallback to ``HEAD``
    would reproduce the defect while reporting success.
    """
    from awgit import stack as stackmod
    from awgit import worktree as wt

    kind = (kind or "feat").strip().lower()
    if kind not in KINDS:
        return 2, "unknown kind " + repr(kind) + " — one of " + ", ".join(KINDS), None
    name = slugify(slug)
    if not name:
        return 2, "a session needs a slug (what is this window for?)", None

    if fetch:
        # Best-effort: an offline box should still cut a branch from the trunk
        # ref it already has, but the base is PRINTED so a stale one is visible.
        _git(cwd, "fetch", "origin", "--quiet")

    trunk = stackmod.detect_trunk(cwd, base)
    if not trunk:
        return 2, ("no trunk found — tried " + ", ".join(stackmod.TRUNK_CANDIDATES)
                   + "; pass --from explicitly"), None
    sha = _ok(cwd, "rev-parse", trunk + "^{commit}")
    if not sha:
        return 2, "trunk " + trunk + " does not resolve to a commit", None
    # `origin/HEAD` is a SYMBOLIC ref, so printing it names no branch — and
    # "which branch did this cut from?" is the whole question the base line
    # exists to answer. Resolve it to the branch it points at.
    if trunk.endswith("HEAD"):
        target = _ok(cwd, "symbolic-ref", "refs/remotes/" + trunk)
        if target:
            trunk = target.replace("refs/remotes/", "") + " (via " + trunk + ")"

    branch = kind + "/" + name
    if _ok(cwd, "rev-parse", "--verify", "refs/heads/" + branch):
        return 1, "branch " + branch + " already exists — pick another slug", None

    ok, msg, path = wt.create(name, cwd=cwd, at=trunk, branch=branch)
    if not ok:
        return 1, msg, path
    if path is not None:
        register(cwd=path, doing=doing)
    return 0, msg + "\n  base: " + trunk + " @ " + sha[:12], path


def selftest() -> int:
    """Prove each rule can still fail. Exit 0 all arms held, 1 an arm did not."""
    import tempfile

    failures: List[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            failures.append(name)

    check("slugify normalises", slugify("Fix The  Thing/Now!") == "fix-the-thing-now")
    check("slugify empty stays empty", slugify("   ") == "")
    check("slugify caps length", len(slugify("x" * 200)) <= 48)
    check("id is stable", session_id(Path("/tmp/a")) == session_id(Path("/tmp/a")))
    check("id separates trees", session_id(Path("/tmp/a")) != session_id(Path("/tmp/b")))

    # 🚨 Every `start()` arm runs against a THROWAWAY repo, never the caller's.
    # The validation arms below refuse before touching git — which is the
    # point of them — but a self-test whose arms only stay harmless while the
    # code under test is correct is not harmless at all: mutating the kind
    # check to `if False:` made an early version of this create a real branch
    # and a real worktree in THIS repo, live, during its own mutation run.
    # A test that can damage the tree it is asserting about will be deleted
    # rather than trusted.
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "r"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "trunkless")
        # 🚨 Assert the REASON, not just the code. Both of these run in a repo
        # with no trunk, so `start()` returns 2 there whatever happens — and
        # an early version checked only the code, which meant deleting the
        # validation entirely still "passed": the refusal simply came from
        # detect_trunk one branch later. Mutation-verified: matching on the
        # message is the only thing that makes these two arms non-vacuous.
        code_k, msg_k, _ = start("x", kind="nope", cwd=repo, fetch=False)
        check("unknown kind refuses with 2", code_k == 2)
        check("unknown kind says WHY", "unknown kind" in msg_k)
        code_s, msg_s, _ = start("", kind="feat", cwd=repo, fetch=False)
        check("empty slug refuses with 2", code_s == 2)
        check("empty slug says WHY", "needs a slug" in msg_s)
        # No trunk ref of any kind exists here, so detect_trunk must fail and
        # start() must exit 2 rather than silently branching from HEAD.
        code, msg, _ = start("thing", cwd=repo, fetch=False)
        check("no trunk exits 2 (not 0)", code == 2)
        check("no trunk says so", "no trunk" in msg or "does not resolve" in msg)
        check("not-a-worktree register fails",
              register(cwd=Path(td) / "absent")[0] is False)
        check("selftest created no worktree",
              not (repo / ".worktrees").exists())

    for name in failures:
        print("  FAIL " + name)
    print("awgit session selftest: " + ("PASS" if not failures else "FAIL")
          + " (" + str(len(failures)) + " failing arm(s))")
    return 0 if not failures else 1
