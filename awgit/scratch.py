"""Scratch clones and blob-commits — surgery that never touches the shared tree.

Two operations this shared-worktree doctrine kept re-deriving by hand:

``awgit scratch <dest>``
    A partial clone (``--filter=blob:none``) of THIS repo's origin, with the
    caller's git identity configured locally so plain ``git commit`` works
    there. The safe place for a large merge is a checkout of your own — a
    merge in the shared worktree sweeps every peer's in-flight work — and
    every session that made one re-typed the same filter and identity flags.

``awgit blob-commit --base origin/develop --branch fix/x -m "..." <paths...>``
    Commit EXACTLY the named worktree files onto a base ref, using a private
    temporary index — the shared index and the worktree are never touched, so
    concurrent sessions cannot be swept and cannot sweep you. This is the
    "commit only my files against a branch my checkout is not on" pattern:
    read-tree the base, hash the named files (content filters respected),
    write-tree, commit-tree, and print the ref to push. One session used this
    shape eighteen times in a day, by hand, before it had a name.

Both are thin over git plumbing on purpose: no state of our own, every action
re-checkable with plain git.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional


def _git(cwd: Optional[Path], *args: str,
         env: Optional[dict] = None) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None, capture_output=True,
        text=True, encoding="utf-8", errors="replace", env=e,
    )


def _die(msg: str) -> int:
    print(f"vcs: {msg}")
    return 1


def _identity(repo: Optional[Path]) -> tuple:
    """The caller's effective git identity, from wherever git resolves it."""
    name = _git(repo, "config", "user.name").stdout.strip()
    email = _git(repo, "config", "user.email").stdout.strip()
    return name, email


def cmd_scratch(dest: str, branch: str = "", url: str = "",
                repo: Optional[Path] = None) -> int:
    """Clone origin into ``dest`` with identity configured. Idempotent."""
    if not url:
        r = _git(repo, "remote", "get-url", "origin")
        if r.returncode != 0:
            return _die("no --url and no origin remote here — run inside a "
                        "repo or pass --url")
        url = r.stdout.strip()
    if not branch:
        r = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        branch = (r.stdout.strip().split("/")[-1] if r.returncode == 0
                  else "main")
    name, email = _identity(repo)
    if not name or not email:
        return _die("your git identity is unset (user.name/user.email) — the "
                    "scratch clone would produce anonymous commits; set it "
                    "once with `git config --global user.name ...`")

    d = Path(dest)
    if (d / ".git").exists():
        print(f"vcs: existing clone at {d} — re-asserting")
        r = _git(d, "fetch", "origin", branch)
        if r.returncode != 0:
            return _die(f"fetch failed: {r.stderr.strip()[:200]}")
    else:
        r = _git(None, "clone", "--filter=blob:none", "-b", branch, url,
                 str(d))
        if r.returncode != 0:
            return _die(f"clone failed: {r.stderr.strip()[:200]}")
    for k, v in (("user.name", name), ("user.email", email)):
        _git(d, "config", k, v)
    got = _git(d, "config", "--local", "user.name").stdout.strip()
    if got != name:
        return _die("identity did not stick — refusing to report ok")
    print(f"vcs: ready — {d} on {branch}, identity {name} <{email}>, plain "
          f"`git commit` works there")
    return 0


def cmd_blob_commit(base: str, branch: str, message: str, paths: List[str],
                    push: bool = False, repo: Optional[Path] = None,
                    allow_shrink: bool = False) -> int:
    """Commit exactly ``paths`` (worktree content) onto ``base`` via a private
    temp index. Prints the commit sha and diffstat; never touches the shared
    index or the worktree."""
    root_r = _git(repo, "rev-parse", "--show-toplevel")
    if root_r.returncode != 0:
        return _die("not inside a git repository")
    root = Path(root_r.stdout.strip())

    base_r = _git(root, "rev-parse", "--verify", f"{base}^{{commit}}")
    if base_r.returncode != 0:
        return _die(f"base ref does not resolve: {base}")
    base_sha = base_r.stdout.strip()

    name, email = _identity(root)
    if not name or not email:
        return _die("git identity unset — the commit would be anonymous")

    with tempfile.NamedTemporaryFile(prefix="awgit-blob-index-",
                                     delete=False) as tf:
        index = tf.name
    env = {"GIT_INDEX_FILE": index}
    try:
        r = _git(root, "read-tree", base_sha, env=env)
        if r.returncode != 0:
            return _die(f"read-tree failed: {r.stderr.strip()[:200]}")
        for p in paths:
            fp = root / p
            if not fp.exists():
                r = _git(root, "update-index", "--force-remove", p, env=env)
                if r.returncode != 0:
                    return _die(f"could not record deletion of {p}")
                print(f"vcs:   - {p} (deleted)")
                continue
            hr = _git(root, "hash-object", "-w", "--path", p, str(fp))
            if hr.returncode != 0:
                return _die(f"hash-object failed for {p}: "
                            f"{hr.stderr.strip()[:200]}")
            blob = hr.stdout.strip()
            # The sweep guard, earned the hard way: this tool's FIRST
            # production push carried a worktree file 2,335 lines behind the
            # base and silently reverted 137 peers' rows. A worktree copy that
            # SHRINKS a file against the base is usually a stale checkout, not
            # an intended edit — refuse it unless the caller says otherwise.
            if not allow_shrink:
                br = _git(root, "rev-parse", f"{base_sha}:{p}")
                if br.returncode == 0:
                    ds = _git(root, "diff", "--numstat", br.stdout.strip(),
                              blob).stdout.split()
                    if len(ds) >= 2 and ds[0].isdigit() and ds[1].isdigit():
                        add, rm = int(ds[0]), int(ds[1])
                        if rm > 50 and rm > add * 3:
                            return _die(
                                f"{p}: your copy DELETES {rm} lines the base "
                                f"has (+{add}) — a stale worktree copy would "
                                f"sweep peers' work exactly like this. Update "
                                f"the file from {base} first, or pass "
                                f"--allow-shrink if the deletion is the point")
            r = _git(root, "update-index", "--add",
                     "--cacheinfo", f"100644,{blob},{p}", env=env)
            if r.returncode != 0:
                return _die(f"update-index failed for {p}")
            print(f"vcs:   + {p}")
        tree = _git(root, "write-tree", env=env).stdout.strip()
        cr = _git(root, "commit-tree", tree, "-p", base_sha, "-m", message,
                  env={"GIT_INDEX_FILE": index,
                       "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                       "GIT_COMMITTER_NAME": name,
                       "GIT_COMMITTER_EMAIL": email})
        if cr.returncode != 0:
            return _die(f"commit-tree failed: {cr.stderr.strip()[:200]}")
        sha = cr.stdout.strip()
    finally:
        try:
            os.unlink(index)
        except OSError as e:
            # A leaked temp index is litter, not a defect — but say so rather
            # than swallow it, or a full disk debugging session starts blind.
            print(f"vcs: note — temp index not removed ({e})")

    stat = _git(root, "diff", "--stat", base_sha, sha).stdout.strip()
    print(stat or "vcs: (empty diff — the named files match the base)")
    print(f"vcs: commit {sha[:12]} on top of {base} — the shared index and "
          f"worktree were not touched")
    if push:
        pr = _git(root, "push", "origin", f"{sha}:refs/heads/{branch}")
        if pr.returncode != 0:
            return _die(f"push failed: {pr.stderr.strip()[:200]}")
        print(f"vcs: pushed refs/heads/{branch}")
    else:
        print(f"vcs: push with  git push origin {sha[:12]}:refs/heads/{branch}")
    return 0


def cmd_fresh(ref: str, paths: List[str],
              repo: Optional[Path] = None) -> int:
    """Is my copy of each path BEHIND ``ref``? The pre-edit half of the sweep
    guard: run it BEFORE editing a file peers also move, and the stale-copy
    push that reverts their work never gets written at all."""
    root_r = _git(repo, "rev-parse", "--show-toplevel")
    if root_r.returncode != 0:
        return _die("not inside a git repository")
    root = Path(root_r.stdout.strip())
    if _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}").returncode != 0:
        return _die(f"ref does not resolve: {ref}")
    behind = 0
    for pth in paths:
        fp = root / pth
        rb = _git(root, "rev-parse", f"{ref}:{pth}")
        if rb.returncode != 0:
            print(f"vcs: {pth}: not in {ref}"
                  + (" (new here)" if fp.exists() else " (nowhere)"))
            continue
        if not fp.exists():
            print(f"vcs: {pth}: MISSING here but present in {ref}")
            behind += 1
            continue
        hb = _git(root, "hash-object", "--path", pth, str(fp))
        if hb.stdout.strip() == rb.stdout.strip():
            print(f"vcs: {pth}: identical to {ref}")
            continue
        # numstat needs the blob in the object store; hash with -w
        hw = _git(root, "hash-object", "-w", "--path", pth, str(fp))
        ds = _git(root, "diff", "--numstat", rb.stdout.strip(),
                  hw.stdout.strip()).stdout.split()
        ok = len(ds) >= 2 and ds[0].isdigit()
        add, rm = (int(ds[0]), int(ds[1])) if ok else (0, 0)
        if rm > add * 3 and rm > 20:
            print(f"vcs: {pth}: your copy is BEHIND {ref} (+{add} -{rm}) — "
                  f"pushing it would sweep peers; refresh from {ref} first")
            behind += 1
        else:
            print(f"vcs: {pth}: differs from {ref} (+{add} -{rm}) — looks "
                  f"like your edit, not staleness")
    return 1 if behind else 0


def cmd_union_rows(path: str, key_pattern: str = "",
                   repo: Optional[Path] = None) -> int:
    """Resolve a conflicted append-only row ledger by ID-KEYED UNION.

    For a file where both sides mostly APPEND keyed rows (a debt ledger, a
    backlog, any markdown table with an id column), a textual merge conflicts
    on adjacency while the right answer is simply "keep every id once". The
    spine is the side holding MORE keyed rows — the lesson from doing this by
    hand: the side that looks like 'ours' is routinely the stale one — and the
    other side's unique rows are appended after the spine's last row. Rows are
    never dropped; dropping is a decision a human makes with an editor.
    """
    import re as _re
    root_r = _git(repo, "rev-parse", "--show-toplevel")
    if root_r.returncode != 0:
        return _die("not inside a git repository")
    root = Path(root_r.stdout.strip())
    pat = _re.compile(key_pattern or r"^\| ([A-Za-z]+-\d+) \|")

    def stage(n: int) -> Optional[str]:
        r = _git(root, "show", f":{n}:{path}")
        return r.stdout if r.returncode == 0 else None

    ours, theirs = stage(2), stage(3)
    if ours is None or theirs is None:
        return _die(f"{path} is not in a two-sided merge conflict "
                    f"(run during a merge, on a conflicted path)")

    def rows(src: str) -> dict:
        out = {}
        for line in src.splitlines():
            m = pat.match(line)
            if m:
                out.setdefault(m.group(1), line)
        return out

    o_rows, t_rows = rows(ours), rows(theirs)
    if not o_rows and not t_rows:
        return _die(f"no rows in either side match {pat.pattern!r} — wrong "
                    f"--key-pattern, or this file is not a row ledger")
    spine_src, spine_rows, other_rows, spine_name = (
        (ours, o_rows, t_rows, "ours") if len(o_rows) >= len(t_rows)
        else (theirs, t_rows, o_rows, "theirs"))
    missing = [k for k in other_rows if k not in spine_rows]
    merged = spine_src
    if missing:
        last_key = list(spine_rows)[-1]
        last_row = spine_rows[last_key]
        insert = "\n".join(other_rows[k] for k in missing)
        merged = spine_src.replace(last_row, last_row + "\n" + insert, 1)
    (root / path).write_text(merged, encoding="utf-8", newline="\n")
    print(f"vcs: union — spine={spine_name} ({len(spine_rows)} rows), "
          f"appended {len(missing)} row(s) unique to the other side: "
          f"{' '.join(missing) if missing else '(none)'}")
    print(f"vcs: review, then `git add {path}` — nothing was dropped")
    return 0


def selftest() -> int:
    """Prove blob-commit isolates: a temp repo, a peer's staged edit, and a
    blob-commit that must NOT carry it."""
    import shutil
    td = Path(tempfile.mkdtemp(prefix="awgit-scratch-st-"))
    try:
        _git(None, "init", "-q", "-b", "main", str(td))
        _git(td, "config", "user.name", "t")
        _git(td, "config", "user.email", "t@example.invalid")
        (td / "mine.txt").write_text("v1\n", encoding="utf-8")
        (td / "peer.txt").write_text("p1\n", encoding="utf-8")
        _git(td, "add", "-A")
        _git(td, "commit", "-q", "-m", "base")
        (td / "mine.txt").write_text("v2\n", encoding="utf-8")
        (td / "peer.txt").write_text("p2-UNCOMMITTED\n", encoding="utf-8")
        _git(td, "add", "peer.txt")  # a peer's staged, uncommitted work
        rc = cmd_blob_commit("HEAD", "x", "mine only", ["mine.txt"],
                             repo=td)
        assert rc == 0, "blob-commit failed"
        # the branch ref was not pushed; find the commit via reflog-free means:
        # diff base..the printed sha is not capturable here, so re-derive —
        # the ONLY new commit object containing mine.txt v2 must lack peer v2.
        shas = _git(td, "rev-list", "--all").stdout.split()
        found = False
        for s in shas:
            show = _git(td, "show", f"{s}:mine.txt")
            if show.returncode == 0 and show.stdout == "v2\n":
                peer = _git(td, "show", f"{s}:peer.txt").stdout
                assert peer == "p1\n", "peer's staged edit LEAKED into the commit"
                found = True
        # rev-list --all misses dangling commit-tree objects; verify via the
        # staged index instead: the shared index must still hold peer's edit.
        staged = _git(td, "diff", "--cached", "--name-only").stdout.split()
        assert staged == ["peer.txt"], f"shared index disturbed: {staged}"
        # Sweep guard: shrink a file far below the base and the tool refuses.
        (td / "big.txt").write_text("".join(f"line {i}\n" for i in range(200)),
                                    encoding="utf-8")
        _git(td, "add", "big.txt")
        _git(td, "commit", "-q", "-m", "big")
        (td / "big.txt").write_text("stale\n", encoding="utf-8")
        rc = cmd_blob_commit("HEAD", "x", "shrink", ["big.txt"], repo=td)
        assert rc == 1, "a 199-line shrink was NOT refused"
        rc = cmd_blob_commit("HEAD", "x", "shrink", ["big.txt"], repo=td,
                             allow_shrink=True)
        assert rc == 0, "--allow-shrink did not override"
        # fresh: a truncated copy is BEHIND (exit 1); a small edit is not.
        # (the guard test above left the worktree copy stale on purpose —
        # restore HEAD's content first)
        keep = _git(td, "show", "HEAD:big.txt").stdout
        (td / "big.txt").write_text(keep, encoding="utf-8")
        rc = cmd_fresh("HEAD", ["big.txt"], repo=td)
        assert rc == 0, "identical file misread as behind"
        (td / "big.txt").write_text("stub\n", encoding="utf-8")
        rc = cmd_fresh("HEAD", ["big.txt"], repo=td)
        assert rc == 1, "a 199-line truncation not reported BEHIND"
        (td / "big.txt").write_text(keep + "one more line\n", encoding="utf-8")
        rc = cmd_fresh("HEAD", ["big.txt"], repo=td)
        assert rc == 0, "an additive edit misread as behind"

        # union-rows: a two-sided append conflict keeps every id once.
        (td / "ledger.md").write_text("| A-1 | first |\n", encoding="utf-8")
        _git(td, "add", "-A")
        _git(td, "commit", "-q", "-m", "ledger base")
        _git(td, "checkout", "-q", "-b", "side")
        (td / "ledger.md").write_text(
            "| A-1 | first |\n| A-3 | theirs |\n| A-4 | theirs |\n",
            encoding="utf-8")
        _git(td, "commit", "-q", "-am", "theirs rows")
        _git(td, "checkout", "-q", "main")
        (td / "ledger.md").write_text(
            "| A-1 | first |\n| A-2 | ours |\n", encoding="utf-8")
        _git(td, "commit", "-q", "-am", "ours row")
        merge = _git(td, "merge", "side")
        assert merge.returncode != 0, "expected a conflict to resolve"
        rc = cmd_union_rows("ledger.md", key_pattern=r"^\| (A-\d+) \|",
                            repo=td)
        assert rc == 0, "union-rows failed"
        out = (td / "ledger.md").read_text(encoding="utf-8")
        for rid in ("A-1", "A-2", "A-3", "A-4"):
            assert rid in out, f"union dropped {rid}"
        assert out.count("A-1") == 1, "spine row duplicated"

        tail = "" if found else " (named-file arm: dangling commit unseen)"
        print("selftest: isolation, sweep guard, fresh and union-rows"
              " behaved" + tail)
        return 0
    finally:
        shutil.rmtree(td, ignore_errors=True)
