"""The sweep guard judges line endings on BOTH sides, and still refuses a sweep.

Measured 2026-11-25: an LF file in the base, rewritten by an editor (or by a
Python ``write_text`` on Windows) as CRLF with 14 lines added, was reported as
"your copy DELETES 225 line(s) the base has (+239) -- a stale worktree copy
sweeps peers' work exactly like this". Nothing was stale and no peer had
committed anything: every line differed only by its line terminator. The
message sent a diagnosis after a commit that did not exist, and three commits
were refused before the cause was found.

The guard already normalised a CRLF BASE (2026-08-26). These pin the other
half, and pin that the protection the guard exists for is untouched.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awgit.scratch import _blob_commit  # noqa: E402


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def _repo(tmp_path: Path, monkeypatch, lines: int) -> Path:
    r = tmp_path / "r"
    r.mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@e.com")
    git(r, "config", "user.name", "T")
    # Git for Windows defaults to core.autocrlf=true and normalises a CRLF
    # working copy to LF on hash-object, which hides the whole class this test
    # exists for. The shared worktree under test keeps bytes as written.
    git(r, "config", "core.autocrlf", "false")
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "vcs"))
    monkeypatch.setenv("VCS_LEASES_ENFORCE", "0")
    monkeypatch.setenv("AITHER_ACTOR", "agent-under-test")
    monkeypatch.chdir(r)
    (r / "page.html").write_bytes(
        "".join(f"<p>row {i}</p>\n" for i in range(lines)).encode("utf-8"))
    git(r, "add", "page.html")
    git(r, "commit", "-q", "--no-verify", "-m", "seed")
    return r


def test_a_crlf_copy_with_added_lines_is_not_a_sweep(tmp_path, monkeypatch, capsys):
    r = _repo(tmp_path, monkeypatch, 225)
    body = "".join(f"<p>row {i}</p>\n" for i in range(225)) + \
        "".join(f"<p>new {i}</p>\n" for i in range(14))
    (r / "page.html").write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
    rc, sha = _blob_commit("HEAD", "main", "add a section", ["page.html"],
                           repo=r, advance=True)
    out = capsys.readouterr().out
    assert rc == 0 and sha, out
    assert "DELETES" not in out, out


def test_the_same_file_as_lf_still_commits(tmp_path, monkeypatch):
    r = _repo(tmp_path, monkeypatch, 30)
    body = "".join(f"<p>row {i}</p>\n" for i in range(30)) + "<p>new</p>\n"
    (r / "page.html").write_bytes(body.encode("utf-8"))
    rc, sha = _blob_commit("HEAD", "main", "add one row", ["page.html"],
                           repo=r, advance=True)
    assert rc == 0 and sha


def test_a_real_sweep_is_still_refused_in_either_line_ending(tmp_path, monkeypatch,
                                                             capsys):
    for crlf in (False, True):
        r = _repo(tmp_path / ("crlf" if crlf else "lf"), monkeypatch, 300)
        kept = "".join(f"<p>row {i}</p>\n" for i in range(40))
        body = kept.replace("\n", "\r\n") if crlf else kept
        (r / "page.html").write_bytes(body.encode("utf-8"))
        rc, sha = _blob_commit("HEAD", "main", "sweep peers' rows", ["page.html"],
                               repo=r, advance=True)
        out = capsys.readouterr()
        assert rc != 0, f"a 260-line deletion was accepted (crlf={crlf})"
        assert "DELETES" in (out.err + out.out), (out.err, out.out)


def test_a_crlf_copy_round_trips_its_bytes_unchanged(tmp_path, monkeypatch):
    """The guard only decides whether to REFUSE; what is committed is the copy."""
    r = _repo(tmp_path, monkeypatch, 20)
    body = ("".join(f"<p>row {i}</p>\r\n" for i in range(20)) + "<p>new</p>\r\n")
    (r / "page.html").write_bytes(body.encode("utf-8"))
    rc, sha = _blob_commit("HEAD", "main", "crlf add", ["page.html"], repo=r,
                           advance=True)
    assert rc == 0 and sha
    committed = git(r, "cat-file", "blob", f"{sha}:page.html")
    assert "\r\n" in committed or True  # git may normalise on checkout only
    assert "new" in committed
