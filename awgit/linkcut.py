"""Cut junctions and symlinks AS LINKS before a directory is deleted.

``git worktree remove --force`` (and most recursive deletes on Windows) walk
THROUGH a directory junction and empty its target — a shared ``node_modules``
linked into a worktree is wiped in the tree it really lives in. Removing every
link first, never descending into one, makes the delete stop at the boundary.
Python-3.10-safe: no ``os.path.isjunction``.
"""

from __future__ import annotations

import os
from typing import List, Tuple

_REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT (junctions and symlinks on Windows)


def _is_link(entry: "os.DirEntry[str]") -> bool:
    if entry.is_symlink():
        return True
    try:
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & _REPARSE)


def unlink_links(root: str) -> Tuple[List[str], List[str]]:
    """Remove every link under ``root`` (target untouched). Returns (cut, stuck)."""
    cut: List[str] = []
    stuck: List[str] = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if e.name == ".git":
                continue
            if _is_link(e):
                try:
                    is_dir = e.is_dir(follow_symlinks=False) or (
                        os.name == "nt" and os.path.isdir(e.path))
                    if is_dir:
                        os.rmdir(e.path)
                    else:
                        os.unlink(e.path)
                    cut.append(e.path)
                except OSError:
                    try:
                        os.unlink(e.path)
                        cut.append(e.path)
                    except OSError:
                        stuck.append(e.path)
            elif e.is_dir(follow_symlinks=False):
                stack.append(e.path)
    return cut, stuck
