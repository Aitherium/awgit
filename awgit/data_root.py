"""VCS data-root resolution (standalone).

The op-log, node registry, body store, and lease stores must all live under
ONE root so a stable node id from any store resolves to the same
``(name, path)`` coordinate space. Standalone awgit keeps the durable store
OUTSIDE the git tree — ``~/.aither/awgit/data`` by default, overridable with
``VCS_DATA_ROOT`` (the same env the in-repo capture hooks use). A passed
``root_path`` preserves the monorepo convention ``<root>/Library/Data/vcs``
for callers that want the store under a project root.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def vcs_data_root(root_path: Optional[str] = None) -> Path:
    """Absolute path of the durable vcs store directory (created lazily).

    Priority: ``VCS_DATA_ROOT`` env → ``root_path/Library/Data/vcs`` when a
    project root is supplied → ``~/.aither/awgit/data``.
    """
    override = os.environ.get("VCS_DATA_ROOT")
    if override:
        return Path(override)
    if root_path:
        return Path(root_path) / "Library" / "Data" / "vcs"
    return Path.home() / ".aither" / "awgit" / "data"
