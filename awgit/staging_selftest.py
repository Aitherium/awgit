"""Self-test for `awgit stage-mine`, built around the bug that produced it.

Every case runs in a throwaway git repo, so the assertions are about real git
behaviour (merge-file, the index, blob round-trips) rather than about mocks.

The case that matters is `two sessions, my change has two separate hunks`: it is
the 2026-08-10 failure exactly — a checker function in one place and the line
registering it somewhere else. The marker-based filter kept the first and dropped
the second, producing a committed check that nothing called.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import List

from awgit.leases import snapshot_baseline
from awgit.staging import StagingError, stage_mine, verify_staged


def _git(args: List[str], repo: Path, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60, **kw,
    )


def _new_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["config", "commit.gpgsign", "false"], repo)
    return repo


def _commit(repo: Path, msg: str = "c") -> None:
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "--no-verify", "-m", msg], repo)


#: A file shaped like the real one: a place for functions, and a registry far
#: away from it. That distance is what defeats a marker heuristic.
ORIGINAL = "\n".join(
    ["def existing():", "    return 1", ""]
    + [f"# filler {i}" for i in range(40)]
    + ["CHECKS = [", '    ("existing", existing),', "]", ""]
)


def run_self_test() -> int:
    failures: List[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"  ok   {name}")
        else:
            print(f"  FAIL {name} {detail}")
            failures.append(name)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo = _new_repo(tmp)
        target = repo / "checker.py"
        target.write_text(ORIGINAL, encoding="utf-8", newline="")
        _commit(repo, "base")

        # ── the real scenario, in the real ORDER ─────────────────────────────
        # This is what actually happened on 2026-08-10: the other session's work
        # was ALREADY in the working tree (uncommitted) when I started. I then
        # take my lease — so the baseline captures the file INCLUDING their work
        # — and add a function AND its registration, two hunks far apart.
        #
        # Order matters and is the contract: the baseline separates "everything
        # that was here before I started" from "what I did". Take the lease
        # before you edit and their prior work is outside your diff by
        # construction; take it after and you have already lost the distinction.
        theirs_first = ORIGINAL.replace(
            "# filler 7", "# filler 7 — other session's podman work"
        )
        target.write_text(theirs_first, encoding="utf-8", newline="")

        baseline = snapshot_baseline("checker.py", repo)
        check("lease captures a baseline blob", bool(baseline), f"got {baseline!r}")

        mine = theirs_first.replace(
            "def existing():",
            "def check_addon_manifest_fields():\n    return []\n\n\ndef existing():",
        ).replace(
            '    ("existing", existing),',
            '    ("existing", existing),\n'
            '    ("addon manifest fields match AddonManager", check_addon_manifest_fields),',
        )
        target.write_text(mine, encoding="utf-8", newline="")

        result = stage_mine("checker.py", baseline, repo)
        check("stages without error", result.staged, result.note)
        check("counts the other session's line as excluded",
              result.foreign_lines_excluded >= 1, f"foreign={result.foreign_lines_excluded}")

        staged = _git(["show", ":checker.py"], repo).stdout
        check("staged copy has my FUNCTION", "def check_addon_manifest_fields" in staged)
        # The assertion the whole command exists for.
        check("staged copy has my REGISTRATION",
              "addon manifest fields match AddonManager" in staged,
              "this is the exact line the marker filter dropped")
        check("staged copy EXCLUDES the other session's edit",
              "other session's podman work" not in staged)
        check("working tree still has the other session's edit",
              "other session's podman work" in target.read_text(encoding="utf-8"))

        # ── the staged BLOB is byte-clean: no CRLF smuggled in by subprocess ──
        # Measured 2026-08-14 on the real repo: hash-object was fed through
        # subprocess TEXT mode, which translates "\n" -> os.linesep on write,
        # so on Windows the staged blob was CRLF on every line while HEAD was
        # LF — a 51-line edit staged as a 718/667 full-file rewrite. Every
        # other read in this file is text=True, which normalizes CRLF back on
        # the way in and therefore CANNOT see the class; this one deliberately
        # reads RAW BYTES. (On POSIX os.linesep is "\n", so the old bug never
        # manifests there — this case earns its keep on Windows, where the
        # fleet actually runs this tool.)
        blob = subprocess.run(
            ["git", "show", ":checker.py"], cwd=repo,
            capture_output=True, timeout=60,
        ).stdout
        cr_count = blob.count(b"\r")
        check("staged blob carries no CR bytes (CRLF smuggling)",
              cr_count == 0,
              f"{cr_count} CR bytes — subprocess text-mode newline translation "
              "has been reintroduced on the hash-object write path")

        # ── --require catches an incomplete stage ────────────────────────────
        absent = verify_staged("checker.py", ["addon manifest fields match AddonManager"], repo)
        check("--require passes when the line is there", not absent, str(absent))
        absent = verify_staged("checker.py", ["a line that was never written"], repo)
        check("--require FAILS when a required line is missing", len(absent) == 1)

        # ── a missing baseline must refuse, not guess ────────────────────────
        try:
            stage_mine("checker.py", "", repo)
            check("refuses with no baseline", False, "it proceeded")
        except StagingError:
            check("refuses with no baseline", True)

        # ── a genuine same-line conflict must refuse, not pick a side ────────
        repo2 = _new_repo(tmp / "b" if (tmp / "b").mkdir() or True else tmp)
        t2 = repo2 / "f.txt"
        t2.write_text("alpha\nbeta\ngamma\n", encoding="utf-8", newline="")
        _commit(repo2, "base")
        base2 = snapshot_baseline("f.txt", repo2)
        # Their commit changes the same line HEAD-side...
        t2.write_text("alpha\nTHEIRS\ngamma\n", encoding="utf-8", newline="")
        _commit(repo2, "theirs")
        # ...and my working edit changes it too, from the same baseline.
        t2.write_text("alpha\nMINE\ngamma\n", encoding="utf-8", newline="")
        try:
            stage_mine("f.txt", base2, repo2)
            check("refuses a same-line conflict", False, "it silently picked a side")
        except StagingError as exc:
            check("refuses a same-line conflict", "conflict" in str(exc).lower())

        # ── the honest limit, asserted rather than hidden ────────────────────
        # An edit another actor makes AFTER your lease lands inside your diff
        # window and is indistinguishable from yours. Path leases are exclusive,
        # so this only happens when the other writer took no lease — and that is
        # the case to state plainly rather than pretend to solve.
        repo3 = _new_repo(tmp / "c" if (tmp / "c").mkdir() or True else tmp)
        t4 = repo3 / "g.txt"
        t4.write_text("one\ntwo\n", encoding="utf-8", newline="")
        _commit(repo3, "base")
        b4 = snapshot_baseline("g.txt", repo3)
        t4.write_text("one\ntwo\nMINE\nUNLEASED-WRITER\n", encoding="utf-8", newline="")
        stage_mine("g.txt", b4, repo3)
        staged4 = _git(["show", ":g.txt"], repo3).stdout
        check("post-lease foreign edits are NOT separable (documented limit)",
              "UNLEASED-WRITER" in staged4,
              "if this ever starts excluding them, update the docstring — the "
              "tool would have gained a capability it currently disclaims")

        # ── no-op when nothing changed since the baseline ────────────────────
        t3 = repo / "quiet.txt"
        t3.write_text("unchanged\n", encoding="utf-8", newline="")
        _commit(repo, "quiet")
        b3 = snapshot_baseline("quiet.txt", repo)
        r3 = stage_mine("quiet.txt", b3, repo)
        check("reports no-op when there are no edits", not r3.staged and "no edits" in r3.note)

    print()
    if failures:
        print(f"SELF-TEST FAILED — {len(failures)}: {', '.join(failures)}")
        return 1
    print("stage-mine self-test passed — your edits survive, theirs stay out, "
          "and an incomplete stage is refused")
    return 0
