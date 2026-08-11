#!/usr/bin/env python3
"""Publish-time moat guard — inspect built artifacts before they ship to PyPI.

Fails (non-zero exit) if the wheel OR the sdist leaks anything that must not
be public. The standalone awgit package is SELF-CONTAINED by contract (stdlib +
httpx only, no AitherOS platform code) — this guard makes the contract
load-bearing at publish time instead of a README claim:

  * a residual AitherOS-platform import — ``from lib.`` / ``import lib.`` /
    ``faculties`` at line-start in any shipped ``.py`` (the AitherOS monorepo's
    awgit is the ONLY place platform internals are allowed);
  * bytecode / caches shipping in the artifact (``__pycache__``, ``.pyc``);
  * a missing LICENSE (the sdist matters as much as the wheel: ``python -m
    build`` publishes BOTH, and an sdist can include files the wheel excludes).

The wheel is the runtime surface; the sdist is what a source-install and the
readme render. Both are checked.

Usage:
    python scripts/check_moat_boundary.py [dist/awgit-*.whl dist/awgit-*.tar.gz ...]

With no argument it picks the newest wheel AND the newest sdist in ``dist/``.
"""

from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from pathlib import Path

# Line-start import statements referencing the AitherOS monorepo namespace.
# Docstring mentions (``lib.faculties.CodeGraph`` as a fidelity note) are NOT
# imports and are deliberately allowed.
_FORBIDDEN_IMPORT = re.compile(
    rb"^[ \t]*(from|import)[ \t]+lib(\.[A-Za-z_][A-Za-z0-9_]*)?\b", re.MULTILINE
)
_FORBIDDEN_IMPORT_FACULTIES = re.compile(
    rb"^[ \t]*(from|import)[ \t]+faculties\b", re.MULTILINE
)


def _newest(pattern: str) -> Path | None:
    dist = Path(__file__).resolve().parent.parent / "dist"
    hits = sorted(dist.glob(pattern), key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


def _members(path: Path) -> list[tuple[str, bytes]]:
    """Yield (normalised-name, bytes) for every file in the archive."""
    out: list[tuple[str, bytes]] = []
    if path.name.endswith(".whl"):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                out.append((info.filename.replace("\\", "/"), zf.read(info)))
    else:
        with tarfile.open(path) as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                f = tf.extractfile(m)
                if f is not None:
                    out.append((m.name.replace("\\", "/"), f.read()))
    return out


def _check(path: Path) -> list[str]:
    violations: list[str] = []
    saw_license = False
    for name, data in _members(path):
        if "__pycache__" in name or name.endswith(".pyc") or name.endswith(".pyo"):
            violations.append(f"bytecode shipped: {name}")
            continue
        # sdist members live under awgit-0.1.0/ — normalise to repo-relative.
        rel = name.split("/", 1)[1] if not name.startswith("awgit/") and "/" in name else name
        if rel.endswith(".py"):
            if _FORBIDDEN_IMPORT.search(data) or _FORBIDDEN_IMPORT_FACULTIES.search(data):
                violations.append(f"AitherOS-platform import in shipped source: {name}")
        # LICENSE lives at the sdist root OR under dist-info/licenses/ in a wheel.
        if name.endswith("LICENSE"):
            saw_license = True
    if not saw_license:
        violations.append(f"no LICENSE in artifact: {path.name}")
    return violations


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    dist = Path(__file__).resolve().parent.parent / "dist"
    if not dist.is_dir():
        print("moat: no dist/ — build first (python -m build)")
        return 2
    targets = [Path(a) for a in argv if Path(a).exists()] or [
        _newest("awgit-*.whl"),
        _newest("awgit-*.tar.gz"),
    ]
    targets = [t for t in targets if t is not None]
    if not targets:
        print("moat: no awgit artifacts found in dist/")
        return 2
    violations: list[str] = []
    for t in targets:
        violations += _check(t)
    if violations:
        print("moat: BLOCKED — the built artifacts must not ship:")
        for v in violations:
            print(f"  ✗ {v}")
        return 1
    print(f"moat: OK — {len(targets)} artifact(s) clean (no platform imports, no bytecode, LICENSE present)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
