"""Worktrees — the answer the rewrite guard points you at.

When another actor is live in your checkout, the fix is not to force the
rewrite, it is to get a checkout of your own. ``git worktree`` already does
that; this wraps it so the refusal can end in a command you can paste, and so
the new tree is created somewhere predictable.

Thin on purpose. Everything here is a ``git worktree`` invocation with a
default path and a readable error — there is no state of our own to keep, and a
worktree registry that could disagree with git's would be worse than none.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

#: Where a new worktree lands unless told otherwise. Inside the repo (and
#: gitignored by convention) so it travels with the checkout and is obvious to
#: find, rather than in a temp dir nobody can locate afterwards.
DEFAULT_PARENT = ".worktrees"


def _git(repo: Optional[Path], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo) if repo else None, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def main_root(cwd: Optional[Path] = None) -> Optional[Path]:
    """The MAIN worktree's root, even when called from inside a linked one.

    ``--show-toplevel`` answers "this worktree"; new worktrees must be created
    relative to the main one, or a worktree made from inside a worktree nests
    and the paths stop being predictable.
    """
    proc = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    common = Path(proc.stdout.strip())
    return common.parent if common.name == ".git" else common


def listing(cwd: Optional[Path] = None) -> List[Tuple[str, str, str]]:
    """(path, sha, branch) for every worktree git knows about."""
    proc = _git(cwd, "worktree", "list", "--porcelain")
    if proc.returncode != 0:
        return []
    out: List[Tuple[str, str, str]] = []
    path = sha = branch = ""
    for line in proc.stdout.splitlines() + [""]:
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("HEAD "):
            sha = line[len("HEAD "):]
        elif line.startswith("branch "):
            branch = line[len("branch "):].replace("refs/heads/", "")
        elif not line.strip() and path:
            out.append((path, sha, branch or "(detached)"))
            path = sha = branch = ""
    return out


def create(name: str, cwd: Optional[Path] = None,
           at: str = "HEAD", branch: str = "",
           sparse: Optional[Sequence[str]] = None) -> Tuple[bool, str, Optional[Path]]:
    """Make a worktree named ``name``. Returns (ok, message, path).

    ``branch`` separates the BRANCH name from the DIRECTORY name, which they
    cannot share once branches are namespaced: ``-b feat/thing`` with the same
    string as the path creates a nested ``.worktrees/feat/thing`` directory,
    so the tree stops being findable at the name its owner used. Default
    (empty) keeps the old behaviour — directory and branch are both ``name``.

    ``sparse`` (repo-relative directories) makes a cheap agent tree: only
    those directories are materialised, through a per-worktree cone-mode
    sparse-checkout. :func:`_create_sparse` says why that path is careful.
    """
    root = main_root(cwd)
    if root is None:
        return False, "not a git worktree", None
    target = root / DEFAULT_PARENT / name
    if target.exists():
        return False, f"{target} already exists", target
    if sparse:
        return _create_sparse(root, target, at, branch or name, sparse)
    proc = _git(cwd, "worktree", "add", "-b", branch or name, str(target), at)
    if proc.returncode != 0:
        # git's own message is the useful one (branch exists, dirty index, ...);
        # replacing it with our own would lose the diagnosis.
        return False, (proc.stderr or proc.stdout).strip(), target
    return True, f"created {target} on branch {branch or name}", target


def _cone_dirs(dirs: Sequence[str]) -> Tuple[List[str], str]:
    """Normalise ``--sparse`` arguments to cone-mode directory names.

    Cone mode takes DIRECTORIES relative to the repo root; a leading ``/``,
    a Windows backslash or a trailing slash is spelling, not meaning, and a
    ``..``, ``.`` or an option-looking name is refused rather than handed to
    git. So is any ``:`` -- a drive letter is never repo-relative, and it is
    what Git Bash's MSYS path conversion makes of ``--sparse /AitherOS/lib``
    (``C:/Program Files/Git/AitherOS/lib``), which git would accept as a
    cone that matches nothing.
    """
    out: List[str] = []
    for raw in dirs:
        d = raw.replace("\\", "/").strip().strip("/")
        parts = d.split("/")
        if (not d or d.startswith("-") or ":" in d
                or ".." in parts or "." in parts):
            hint = (" (a drive letter: under Git Bash set MSYS_NO_PATHCONV=1 "
                    "or drop the leading /)" if ":" in d else "")
            return [], f"invalid --sparse directory {raw!r}{hint}"
        if d not in out:
            out.append(d)
    if not out:
        return [], "--sparse needs at least one directory"
    return out, ""


def _missing_dirs(root: Path, at: str, cone: Sequence[str]) -> List[str]:
    """The cone directories that are NOT a directory in the tree at ``at``.

    git takes any cone name, existing or not, and the result is a tree with
    only root-level files that still reads as a success. Asking the object
    store first (``cat-file -t <at>:<dir>`` must say ``tree``) turns a typo or
    a file path into a refusal before anything is created.
    """
    missing: List[str] = []
    for d in cone:
        kind = _git(root, "cat-file", "-t", f"{at}:{d}")
        if kind.returncode != 0 or kind.stdout.strip() != "tree":
            missing.append(d)
    return missing


def _common_config(root: Path) -> Path:
    """The shared config file every worktree reads (``<common-dir>/config``)."""
    proc = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    common = Path(proc.stdout.strip()) if proc.returncode == 0 else root / ".git"
    return common / "config"


def index_ok(target: Path) -> Tuple[bool, str]:
    """Is the tree's index populated AND equal to HEAD?

    A ``--no-checkout`` worktree starts with an EMPTY index: every tracked
    file then reads as a staged deletion, and the first commit made there
    deletes the repository on that branch. So this asks two things a
    half-made tree cannot both satisfy: ``ls-files`` lists something, and
    ``diff --cached`` against HEAD is empty (deletions included).
    Skip-worktree entries stay in the index, so a sparse tree that put one
    directory on disk still passes — that is the point of sparse.
    """
    files = _git(target, "ls-files")
    count = len([ln for ln in files.stdout.splitlines() if ln.strip()])
    if files.returncode != 0 or count == 0:
        return False, f"index is EMPTY in {target} (ls-files count {count})"
    staged = _git(target, "diff", "--cached", "--quiet", "HEAD")
    if staged.returncode != 0:
        return False, (f"index of {target} differs from HEAD ({count} entries) "
                       f"— a commit there would change files nobody edited")
    return True, f"index populated ({count} entries)"


def _create_sparse(root: Path, target: Path, at: str, branch: str,
                   dirs: Sequence[str]) -> Tuple[bool, str, Optional[Path]]:
    """``worktree add --no-checkout`` + per-worktree cone sparse + populate.

    Order matters and every step is checked:

    1. ``--no-checkout`` so git does not first write the whole tree we are
       about to hide (the cost this exists to avoid).
    2. ``extensions.worktreeConfig`` so ``core.sparseCheckout`` lands in THIS
       worktree's ``config.worktree``, never the shared ``.git/config`` — set
       there it would switch sparse on for the main checkout and every peer.
       The pattern file already lives in the worktree's own admin dir
       (``.git/worktrees/<name>/info/sparse-checkout``).
    3. ``read-tree -mu HEAD`` fills the index (skip-worktree outside the
       cone) and writes only the cone to disk.
    4. :func:`index_ok` — a tree whose index is empty or differs from HEAD
       is torn down and reported, never handed back.
    """
    cone, err = _cone_dirs(dirs)
    if err:
        return False, err, None
    missing = _missing_dirs(root, at, cone)
    if missing:
        return False, (f"--sparse directory not in the tree at {at}: "
                       f"{', '.join(missing)}"), None
    # Turning worktreeConfig on changes what a shared core.worktree /
    # core.bare=true applies to (git then reads them for the main tree only).
    # Refuse rather than silently move somebody's checkout.
    shared = str(_common_config(root))
    for key in ("core.worktree", "core.bare"):
        val = _git(root, "config", "--file", shared, key).stdout.strip()
        if val and val != "false":
            return False, (f"refusing --sparse: {key}={val} is set in the shared "
                           f"config; enabling extensions.worktreeConfig would "
                           f"change which checkout it applies to"), None
    proc = _git(root, "worktree", "add", "--no-checkout", "-b", branch, str(target), at)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip(), target

    def _fail(why: str) -> Tuple[bool, str, Optional[Path]]:
        # The tree is seconds old with no work in it; leaving it behind is
        # leaving the empty-index trap armed. Remove it and the branch just
        # cut (it points at ``at`` and carries nothing of its own).
        # Each undo is checked: a Windows file lock can stop the remove, and
        # a message claiming "rolled back" over a half-made tree is the trap.
        left: List[str] = []
        rm = _git(root, "worktree", "remove", "--force", str(target))
        if rm.returncode != 0 or target.exists():
            err = (rm.stderr or rm.stdout).strip() or "still on disk"
            left.append(f"worktree {target} ({err}) — EMPTY INDEX, do not "
                        f"commit there; `git worktree remove --force {target}`")
        br = _git(root, "branch", "-D", branch)
        if br.returncode != 0:
            left.append(f"branch {branch} ({(br.stderr or br.stdout).strip()})")
        if left:
            return False, (f"sparse worktree NOT created — {why}; rollback "
                           f"INCOMPLETE, left behind: {'; '.join(left)}"), target
        return False, f"sparse worktree NOT created — {why} (rolled back)", target

    steps: List[Tuple[Path, Tuple[str, ...]]] = []
    # The shared config is written only when it must be: many sessions share
    # it, and a needless write is a `could not lock config file` for a peer
    # (2 of 6 parallel creates failed that way when this always wrote).
    wtc = _git(root, "config", "--file", shared, "--type=bool", "--get",
               "extensions.worktreeConfig").stdout.strip()
    if wtc != "true":
        steps.append((root, ("config", "extensions.worktreeConfig", "true")))
    steps += [
        (target, ("sparse-checkout", "init", "--cone")),
        (target, ("sparse-checkout", "set", "--cone", *cone)),
        (target, ("read-tree", "-mu", "HEAD")),
    ]
    for where, step in steps:
        res = _git(where, *step)
        if res.returncode != 0:
            return _fail(f"`git {' '.join(step)}` failed: "
                         f"{(res.stderr or res.stdout).strip()}")
    ok, why = index_ok(target)
    if not ok:
        return _fail(why)
    return True, (f"created {target} on branch {branch} "
                  f"(sparse: {', '.join(cone)}; {why})"), target


#: Env override for :func:`reap_tool`, then the places a repo conventionally
#: keeps its dev tooling (relative to the main worktree root).
REAP_TOOL_ENV = "AWGIT_REAP_TOOL"
REAP_TOOL_NAME = "reap_worktrees.py"
REAP_TOOL_GLOBS = ("dev/tools/", "tools/", "scripts/", "*/dev/tools/")


def reap_tool(cwd: Optional[Path] = None) -> Optional[Path]:
    """The repo's own stale-worktree reaper, when this checkout ships one.

    ``awgit worktree reap`` delegates instead of re-implementing the safety
    rules (clean status, reachable HEAD, idle time): a second reaper with its
    own idea of "safe" is how work gets deleted. ``$AWGIT_REAP_TOOL`` names
    it explicitly; otherwise a ``reap_worktrees.py`` in a conventional tools
    directory is used. ``None`` means there is none and the caller says so.
    """
    explicit = os.environ.get(REAP_TOOL_ENV, "").strip()
    if explicit:
        return Path(explicit) if Path(explicit).is_file() else None
    root = main_root(cwd)
    if root is None:
        return None
    for pattern in REAP_TOOL_GLOBS:
        hits = sorted(root.glob(pattern + REAP_TOOL_NAME))
        if hits:
            return hits[0]
    return None


def _is_zombie(path: Path, cwd: Optional[Path]) -> bool:
    """True when ``path`` looks like a worktree directory but git has no
    record of it AND it has no local ``.git`` pointer of its own — the shape
    that traps a later ``cd`` into it: any git command run there finds no
    local ``.git``, walks up, and silently resolves to whatever repo happens
    to contain it, which is why this is asserted rather than assumed.
    to contain it.
    """
    if not path.is_dir() or (path / ".git").exists():
        return False
    registered = {Path(p).resolve() for p, _, _ in listing(cwd)}
    return path.resolve() not in registered


def remove(name_or_path: str, cwd: Optional[Path] = None,
           force: bool = False) -> Tuple[bool, str]:
    """Remove a worktree. Never forces unless asked — it can discard work.

    ``git worktree remove`` is NOT atomic on the filesystem: it unregisters
    the worktree (deletes its ``.git`` pointer, drops the admin entry under
    the main repo's ``.git/worktrees/``) and THEN deletes the working
    directory's contents. If step two fails partway — a locked file, a
    shell still sitting inside the directory as cwd, a permission error on
    Windows — git reports the failure, but the worktree is ALREADY
    unregistered and its ``.git`` pointer is ALREADY gone. What is left is a
    zombie: unregistered, pointerless, indistinguishable from a real
    directory to anything that does not check `git worktree list`. A `cd`
    into it later resolves every git command to the PARENT repo instead,
    silently — this is exactly how a `git reset --hard` believed to be
    scoped to an isolated worktree once ran against the shared main repo.

    So a failed removal here is followed by a check: did it leave a zombie?
    If yes, that is reported LOUDLY and distinctly from an ordinary failure
    (which leaves the worktree intact and registered) — never silently, and
    never conflated with "removal failed, nothing changed".
    """
    root = main_root(cwd)
    candidate = Path(name_or_path)
    if not candidate.is_absolute() and root is not None:
        guess = root / DEFAULT_PARENT / name_or_path
        if guess.exists():
            candidate = guess
    args = ["worktree", "remove"] + (["--force"] if force else []) + [str(candidate)]
    proc = _git(cwd, *args)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        if _is_zombie(candidate, cwd):
            return False, (
                f"PARTIAL REMOVAL — {candidate} is now a ZOMBIE: unregistered "
                f"with git, no local .git pointer, but the directory still "
                f"exists on disk. Do NOT cd into it — any git command run "
                f"there will silently operate on {root or 'the parent repo'} "
                f"instead. Original error: {err}. Fix (both steps — `git "
                f"worktree remove --force` on a pointerless directory itself "
                f"fails with 'gitdir file points to non-existent location', "
                f"verified live): `git worktree prune` to drop the stale "
                f"admin entry, THEN delete the directory by path "
                f"(`rm -rf {candidate}`) once you have confirmed nothing of "
                f"value remains in it."
            )
        return False, err
    return True, f"removed {candidate}"
