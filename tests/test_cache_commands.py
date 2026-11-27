"""`awgit caches` and `awgit maintenance`, through the real CLI, against files REAL git wrote.

The deeper fabricated-bytes suite is dev/tests/test_awgit_cache_integrity.py. This one
exists for a different reason: a checker whose idea of the format came from its own
author could agree with itself and disagree with git. So here the multi-pack-index and
commit-graph are produced by ``git multi-pack-index write`` / ``git commit-graph
write`` in a throwaway repo (sha1 and, where this git supports it, sha256), and must
pass; the same files truncated or patched must fail, and ``git multi-pack-index verify``
must agree on the truncated one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit import cache_integrity as ci  # noqa: E402
from awgit import cli  # noqa: E402


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    p = subprocess.run(["git", "-c", "user.email=t@e.com", "-c", "user.name=T", *args],
                       cwd=str(repo), capture_output=True, text=True, env=env)
    if check and p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {p.stderr}")
    return p


def make_repo(tmp_path: Path, fmt: str) -> Path:
    r = tmp_path / f"repo-{fmt}"
    r.mkdir()
    init = subprocess.run(["git", "init", "-q", f"--object-format={fmt}"], cwd=r, capture_output=True)
    if init.returncode != 0:
        pytest.skip(f"this git cannot init --object-format={fmt}")
    for i in range(3):
        (r / f"f{i}.txt").write_text(f"{i}\n" * 50)
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", f"c{i}")
        git(r, "repack", "-q")          # a new small pack per round: several packs to index
    git(r, "multi-pack-index", "write")
    git(r, "commit-graph", "write", "--reachable")
    return r


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("AWGIT_CACHE_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("AITHER_MAINT_LOCK", str(tmp_path / "none.lock"))
    monkeypatch.delenv("AWGIT_CACHE_GUARD", raising=False)
    # a peer on this host may be running a real git writer; these tests are about OUR logic
    from awgit import cache_maintenance as _cm
    monkeypatch.setattr(_cm, "writers_running", lambda proc="/proc": [])


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
def test_files_written_by_real_git_pass_quick_and_deep(tmp_path, fmt, capsys):
    r = make_repo(tmp_path, fmt)
    gd = r / ".git"
    assert (gd / "objects/pack/multi-pack-index").exists() and (gd / "objects/info/commit-graph").exists()
    for deep in (False, True):
        states = {v["kind"]: v["state"] for v in ci.check_caches(gd, deep=deep)}
        assert states == {ci.MIDX: ci.OK, ci.COMMIT_GRAPH: ci.OK}, (deep, states)
    assert cli.main(["caches", "--repo", str(r), "--deep", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["exit"] == 0


def test_split_commit_graph_chain_written_by_real_git(tmp_path):
    r = make_repo(tmp_path, "sha1")
    gd = r / ".git"
    (gd / "objects/info/commit-graph").unlink()
    git(r, "commit-graph", "write", "--reachable", "--split")
    chain = gd / "objects/info/commit-graphs/commit-graph-chain"
    assert chain.exists()
    assert all(v["state"] == ci.OK for v in ci.check_caches(gd, deep=True) if v["state"] != ci.ABSENT)


def test_truncated_midx_is_bad_and_git_agrees(tmp_path, capsys):
    r = make_repo(tmp_path, "sha1")
    midx = r / ".git/objects/pack/multi-pack-index"
    data = midx.read_bytes()
    midx.write_bytes(data[:-25])
    assert ci.check_chunk_file(midx, ci.MIDX)["state"] == ci.BAD
    assert git(r, "multi-pack-index", "verify", check=False).returncode != 0, "git agrees it is broken"
    assert cli.main(["caches", "--repo", str(r)]) == 1
    assert midx.exists(), "read-only without --fix"
    assert cli.main(["caches", "--repo", str(r), "--fix"]) == 0
    assert not midx.exists() and list(midx.parent.glob("multi-pack-index.bad-*"))
    assert git(r, "log", "--oneline", check=False).returncode == 0, "git falls back to the per-pack indexes"
    assert git(r, "fsck", "--no-dangling", check=False).returncode == 0, "no object was touched"


def test_awgit_git_log_warns_but_does_not_mutate_by_default(tmp_path, capfd):
    r = make_repo(tmp_path, "sha1")
    midx = r / ".git/objects/pack/multi-pack-index"
    midx.write_bytes(midx.read_bytes()[:-25])
    from awgit import git as awgit_git

    assert awgit_git.run(["log", "--oneline"], cwd=str(r)) in (0, 128)
    err = capfd.readouterr().err
    assert "cache guard" in err and str(midx) in err
    assert midx.exists()


def test_maintenance_dry_run_through_the_cli_changes_nothing(tmp_path, capsys):
    r = make_repo(tmp_path, "sha1")
    gd = r / ".git"
    junk = gd / "objects/pack/tmp_pack_dead"
    junk.write_bytes(b"x" * 10)
    old = junk.stat().st_mtime - 40 * 3600
    os.utime(junk, (old, old))
    assert cli.main(["maintenance", "--repo", str(r)]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "tmp_pack_dead" in out and junk.exists()
    assert not (tmp_path / "ledger.jsonl").exists()
    assert cli.main(["maintenance", "--repo", str(r), "--apply"]) == 0
    assert not junk.exists()


# ── round 2: commit-graph step, shallow and promisor repos, all with real git ─

from awgit import cache_maintenance as cm  # noqa: E402


def test_split_commit_graph_appends_a_layer_and_keeps_the_filters(tmp_path):
    r = make_repo(tmp_path, "sha1")
    gd = r / ".git"
    (gd / "objects/info/commit-graph").unlink()
    git(r, "commit-graph", "write", "--reachable", "--changed-paths", "--split=replace")
    assert len(ci.commit_graph_layers(gd)) == 1
    (r / "new.txt").write_text("n\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "more")
    res = cm.apply(gd, commit_graph=True)
    cg = res["commit_graph"]
    assert res["ok"] and cg["ok"], cg
    assert cg["layers_before"] == 1 and cg["layers"] == 2, "an incremental layer on top of the chain"
    assert cg["filters"] is True and cg["verify_rc"] == 0
    assert all(x["filters"] for x in ci.commit_graph_layers(gd))
    assert git(r, "commit-graph", "verify", check=False).returncode == 0
    # --full replaces the chain with one layer, filters intact
    res = cm.apply(gd, commit_graph=True, full=True)
    assert res["ok"] and res["commit_graph"]["layers"] == 1 and res["commit_graph"]["filters"]


def test_shallow_clone_is_flagged_and_graph_step_is_skipped(tmp_path, capsys):
    src = make_repo(tmp_path, "sha1")
    dst = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", src.as_uri(), str(dst)], check=True, capture_output=True)
    assert (dst / ".git/shallow").exists()
    assert cli.main(["caches", "--repo", str(dst), "--json"]) == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["shallow"]["shallow"] is True and any(f["kind"] == "shallow" for f in rep["findings"])
    assert cli.main(["maintenance", "--repo", str(dst), "--apply", "--commit-graph"]) == 1
    out = capsys.readouterr().out
    assert "NOT DONE" in out and "shallow" in out
    assert not (dst / ".git/objects/info/commit-graph").exists()
    assert not (dst / ".git/objects/info/commit-graphs").exists(), "awgit did not run git commit-graph write"
    # git itself, for the record: it exits 0 and writes nothing in a shallow repo
    assert git(dst, "commit-graph", "write", "--reachable", check=False).returncode == 0
    assert not (dst / ".git/objects/info/commit-graph").exists()


def test_promisor_repo_takes_the_midx_path_for_real(tmp_path):
    r = make_repo(tmp_path, "sha1")
    gd = r / ".git"
    git(r, "config", "remote.origin.url", "file:///nonexistent")
    git(r, "config", "remote.origin.promisor", "true")
    git(r, "config", "remote.origin.partialclonefilter", "blob:limit=209715200")
    before = ci.pack_count(gd)
    assert before >= 3
    res = cm.apply(gd, repack=True, batches=[2048])
    rp = res["repack"]
    assert res["ok"] and rp["ok"] and rp["mode"] == "midx-batch", rp
    assert rp["rounds"][0]["verify_rc"] == 0 and rp["rounds"][0]["verify_after_expire_rc"] == 0
    assert ci.pack_count(gd) <= before + 1
    assert git(r, "multi-pack-index", "verify", check=False).returncode == 0
    assert git(r, "log", "--oneline", check=False).returncode == 0
    assert git(r, "fsck", "--no-dangling", check=False).returncode == 0, "no object lost"
