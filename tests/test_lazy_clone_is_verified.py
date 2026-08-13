"""A lazy clone must be PROVEN lazy, not assumed from the flag we passed.

`git clone --filter=blob:none` succeeds and prints a warning when the server
declines to filter. You get a full clone and exit 0 — so a tool that reports
"lazy" because it passed the flag teaches people the feature works when it did
not, and the first they learn otherwise is a disk filling up.

So the claim is read back out of the repository's own config. These tests use a
`file://` URL because local-path clones bypass the transport that implements
filtering; that detail is exactly the kind of thing that makes a filter silently
not apply, which is what the verification exists for.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit import lazy  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    git(src, "config", "user.email", "t@e.com")
    git(src, "config", "user.name", "T")
    for directory in ("alpha", "beta"):
        (src / directory).mkdir()
        (src / directory / "mod.py").write_text(
            f"def {directory}():\n    return 1\n", encoding="utf-8")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "seed")
    return src


def test_a_filtered_clone_reports_itself_as_lazy(origin: Path, tmp_path: Path):
    dest = tmp_path / "lazyclone"
    ok, messages, status = lazy.clone(origin.as_uri(), str(dest))
    assert ok, messages
    assert status.partial, f"the filter did not apply: {status.to_dict()}"
    assert status.promisor, "no promisor remote — missing blobs could not be fetched"
    assert status.lazy


def test_verify_reads_the_repo_not_the_flag(origin: Path, tmp_path: Path):
    """An ORDINARY clone must report NOT lazy, however it was made."""
    dest = tmp_path / "fullclone"
    subprocess.run(["git", "clone", "-q", origin.as_uri(), str(dest)], check=True)
    status = lazy.verify(dest)
    assert not status.partial and not status.lazy, (
        "a full clone reported itself as lazy — the check is reading the flag, "
        "not the repository")


def test_a_sparse_clone_materialises_only_what_was_asked_for(
        origin: Path, tmp_path: Path):
    dest = tmp_path / "sparseclone"
    ok, messages, status = lazy.clone(origin.as_uri(), str(dest), paths=["alpha"])
    assert ok, messages
    assert status.sparse and status.cone
    assert (dest / "alpha" / "mod.py").exists()
    assert not (dest / "beta").exists(), (
        "beta was materialised despite not being requested")


def test_widening_brings_a_directory_in(origin: Path, tmp_path: Path):
    dest = tmp_path / "widen"
    lazy.clone(origin.as_uri(), str(dest), paths=["alpha"])
    assert not (dest / "beta").exists()

    ok, message = lazy.widen(["beta"], dest)
    assert ok, message
    assert (dest / "beta" / "mod.py").exists(), "widening did not materialise beta"
    assert (dest / "alpha" / "mod.py").exists(), "widening dropped what was there"


def test_clone_refuses_a_non_empty_destination(origin: Path, tmp_path: Path):
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "keepme.txt").write_text("mine\n", encoding="utf-8")
    ok, messages, _ = lazy.clone(origin.as_uri(), str(dest))
    assert not ok and "not empty" in messages[0]
    assert (dest / "keepme.txt").exists(), "a refused clone must not touch the tree"


def test_measure_reports_a_real_object_store_size(origin: Path, tmp_path: Path):
    dest = tmp_path / "measured"
    lazy.clone(origin.as_uri(), str(dest))
    sizes = lazy.measure(dest)
    assert sizes["objects_bytes"] > 0, "an empty object store means measure is lying"
