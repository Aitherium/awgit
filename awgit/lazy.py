"""Instant clone at any size — without a filesystem driver.

garden materialises files on demand from a server-backed virtual filesystem.
That is a real capability and this is deliberately NOT an attempt at it: a VFS
means a FUSE mount on Linux and ProjFS on Windows, and Microsoft — who built
ProjFS and VFSforGit — retired that approach and moved to Scalar, which is
``git clone --filter=blob:none`` plus sparse-checkout. Reimplementing the thing
its own author walked away from would trade a supported git feature for a
kernel-adjacent dependency on the platform where it is least reliable.

So the user-visible property is delivered with upstream git: history and trees
come down immediately, file CONTENT is fetched the first time something reads
it, and the working tree only contains the directories you asked for.

**The claim is verified, never assumed.** ``git clone --filter`` succeeds and
prints a warning when a server declines to filter — you get a full clone, exit
0, and a tool cheerfully reporting "lazy". :func:`verify` asks the repository
what it actually is, so ``awgit clone --lazy`` can only report lazy when the
filter is really in the config and the promisor remote really exists.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

BLOB_FILTER = "blob:none"


@dataclass
class Status:
    """What this checkout actually IS, rather than how it was asked for."""

    partial: bool
    filter_spec: str
    promisor: bool
    sparse: bool
    cone: bool
    directories: List[str]

    def to_dict(self) -> dict:
        return {"partial": self.partial, "filter": self.filter_spec,
                "promisor": self.promisor, "sparse": self.sparse,
                "cone": self.cone, "directories": self.directories}

    @property
    def lazy(self) -> bool:
        """Both halves. A filter with no promisor remote cannot backfill."""
        return self.partial and self.promisor


def _git(repo: Optional[Path], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo) if repo else None, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )


def _config(repo: Optional[Path], key: str) -> str:
    proc = _git(repo, "config", "--get", key)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def verify(repo: Optional[Path] = None) -> Status:
    """Ask the repository what it is. Never infer it from the command we ran."""
    filter_spec = _config(repo, "remote.origin.partialclonefilter")
    promisor = _config(repo, "remote.origin.promisor") == "true"
    sparse = _config(repo, "core.sparsecheckout") == "true"
    cone = _config(repo, "core.sparsecheckoutcone") == "true"
    dirs: List[str] = []
    if sparse:
        proc = _git(repo, "sparse-checkout", "list")
        if proc.returncode == 0:
            dirs = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return Status(partial=bool(filter_spec), filter_spec=filter_spec,
                  promisor=promisor, sparse=sparse, cone=cone, directories=dirs)


def clone(url: str, dest: str, paths: Optional[List[str]] = None,
          filter_spec: str = BLOB_FILTER) -> Tuple[bool, List[str], Status]:
    """Clone lazily. Returns (ok, messages, what-it-actually-is)."""
    messages: List[str] = []
    target = Path(dest)
    if target.exists() and any(target.iterdir()):
        return False, [f"{dest} exists and is not empty"], verify(None)

    args = ["clone", f"--filter={filter_spec}"]
    if paths:
        args.append("--sparse")
    args += [url, dest]
    proc = subprocess.run(["git", *args], text=True, encoding="utf-8",
                          errors="replace", capture_output=True)
    if proc.returncode != 0:
        return False, [(proc.stderr or proc.stdout).strip()], verify(None)
    messages.append(f"cloned {url} -> {dest}")

    if paths:
        set_proc = _git(target, "sparse-checkout", "set", *paths)
        if set_proc.returncode != 0:
            messages.append(f"sparse-checkout set failed: {set_proc.stderr.strip()}")
        else:
            messages.append(f"working tree limited to: {', '.join(paths)}")

    # commit-graph makes `log`/`merge-base` fast on a big history, and the
    # stack commands ask those constantly.
    _git(target, "commit-graph", "write", "--reachable")

    status = verify(target)
    if not status.lazy:
        # The clone SUCCEEDED and is simply not lazy — most often because the
        # server declined to filter. Saying so is the whole point: a tool that
        # reports lazy here teaches people the feature works when it did not.
        messages.append(
            "NOT lazy: the server did not honour the filter, so this is a full "
            "clone. Everything works; it just downloaded the whole history.")
    return True, messages, status


def widen(paths: List[str], repo: Optional[Path] = None) -> Tuple[bool, str]:
    """Add directories to a sparse checkout."""
    status = verify(repo)
    if not status.sparse:
        proc = _git(repo, "sparse-checkout", "set", *paths)
    else:
        proc = _git(repo, "sparse-checkout", "add", *paths)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()
    return True, f"materialised: {', '.join(paths)}"


def measure(repo: Optional[Path] = None) -> dict:
    """On-disk size of the object store — the number the claim is about."""
    base = Path(repo or ".")
    git_dir = _git(base, "rev-parse", "--absolute-git-dir").stdout.strip()
    objects = Path(git_dir) / "objects" if git_dir else base / ".git" / "objects"
    total = 0
    if objects.is_dir():
        for path in objects.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    return {"objects_bytes": total, "objects_mb": round(total / 1_048_576, 1),
            "disk_free_mb": round(shutil.disk_usage(base).free / 1_048_576)}


def render(status: Status, sizes: Optional[dict] = None) -> List[str]:
    lines = [f"  lazy:     {'yes' if status.lazy else 'NO'}",
             f"  filter:   {status.filter_spec or '(none — full clone)'}",
             f"  promisor: {'yes' if status.promisor else 'no'}",
             f"  sparse:   {'yes' if status.sparse else 'no'}"
             + (f" (cone, {len(status.directories)} dir(s))" if status.cone else "")]
    for directory in status.directories[:10]:
        lines.append(f"      {directory}")
    if sizes:
        lines.append(f"  objects:  {sizes['objects_mb']} MB on disk")
    if not status.lazy and status.partial:
        lines.append("  note: a filter is configured but there is no promisor remote, "
                     "so missing blobs cannot be fetched on demand")
    return lines
