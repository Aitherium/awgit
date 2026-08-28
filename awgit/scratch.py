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
import sys
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


def _die2(msg: str) -> tuple:
    """_die for the callers that return (rc, sha)."""
    print(f"vcs: {msg}")
    return 1, ""


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


def _index_blob(root: Path, path: str) -> Optional[str]:
    """The blob the SHARED index holds for ``path``, or None if it has no entry."""
    r = _git(root, "ls-files", "-s", "--", path)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    parts = r.stdout.split()
    return parts[1] if len(parts) >= 2 else None


def _tree_blob(root: Path, ref: str, path: str) -> Optional[str]:
    r = _git(root, "rev-parse", f"{ref}:{path}")
    return r.stdout.strip() if r.returncode == 0 else None


def _reconcile_index(root: Path, base_sha: str, new_sha: str,
                     paths: List[str]) -> tuple:
    """Make the SHARED index agree with a commit this tool just made, for
    exactly ``paths`` — and refuse any path where a peer has staged work.

    WHY THIS IS NEEDED AT ALL. blob-commit builds through a private index and
    never touches the shared one, which is the entire point. That is correct
    right up until the local branch MOVES onto the commit: HEAD then contains a
    file the index has no entry for, and git renders that as ``D `` — a staged
    DELETION — beside a ``??`` for the same path. A peer running a plain
    ``git commit`` at that moment deletes files that belong in the tree.
    Reproduced from a clean repo: ``?? new-file.txt`` before the branch moves,
    ``D  new-file.txt`` + ``??`` after.

    WHY IT IS SAFE, which is the part that matters. The isolation guarantee is
    that a peer's staged work is never disturbed. So a path is reconciled ONLY
    when the index still holds exactly what the BASE held — meaning nobody has
    staged anything for it. Any other index state is a peer mid-edit, and it is
    left alone and reported. That check is what keeps this from becoming the
    sweep the private index exists to prevent.
    """
    done, skipped = [], []
    for p in paths:
        old = _tree_blob(root, base_sha, p)
        new = _tree_blob(root, new_sha, p)
        idx = _index_blob(root, p)
        if idx != old:
            skipped.append((p, "a peer has staged work here; left untouched"))
            continue
        if new is None:
            if idx is None:
                continue
            r = _git(root, "update-index", "--force-remove", p)
            if r.returncode != 0:
                skipped.append((p, "could not record the deletion"))
                continue
            done.append(p)
            continue
        if idx == new:
            continue
        r = _git(root, "update-index", "--add", "--cacheinfo",
                 f"100644,{new},{p}")
        if r.returncode != 0:
            skipped.append((p, "update-index refused"))
            continue
        done.append(p)
    return done, skipped


def cmd_reconcile_index(repo: Optional[Path] = None, apply: bool = False) -> int:
    """Find — and with --apply, repair — PHANTOM staged deletions.

    A phantom is a path the index reports deleted while the file is present in
    the worktree and byte-identical to HEAD. Nothing is being lost by making the
    index agree: it is the residue of a branch that moved onto a commit the
    index never saw.

    It is NOT the same as a real ``git rm --cached``, where someone deliberately
    staged a removal. Those are indistinguishable from the outside when the file
    is also identical to HEAD, which is exactly why this REPORTS by default and
    repairs only when asked.
    """
    root_r = _git(repo, "rev-parse", "--show-toplevel")
    if root_r.returncode != 0:
        return _die("not inside a git repository")
    root = Path(root_r.stdout.strip())
    r = _git(root, "diff", "--cached", "--name-only", "--diff-filter=D")
    if r.returncode != 0:
        return _die("could not read the staged deletions")
    phantom, genuine = [], 0
    for p in [x for x in r.stdout.splitlines() if x.strip()]:
        fp = root / p
        if not fp.exists():
            genuine += 1
            continue
        head = _tree_blob(root, "HEAD", p)
        if head is None:
            genuine += 1
            continue
        hb = _git(root, "hash-object", "--path", p, str(fp))
        if hb.returncode == 0 and hb.stdout.strip() == head:
            phantom.append((p, head))
        else:
            genuine += 1
    if not phantom:
        print(f"vcs: no phantom staged deletions ({genuine} genuine, left alone)")
        return 0
    for p, _b in phantom:
        print(f"vcs:   phantom {p}")
    print(f"vcs: {len(phantom)} phantom, {genuine} genuine (never touched)")
    if not apply:
        print("vcs: nothing changed — pass --apply to make the index agree "
              "with HEAD for the phantom paths only")
        return 0
    fixed = 0
    for p, blob in phantom:
        if _git(root, "update-index", "--add", "--cacheinfo",
                f"100644,{blob},{p}").returncode == 0:
            fixed += 1
    left = [p for p, _ in phantom
            if p in _git(root, "diff", "--cached", "--name-only",
                         "--diff-filter=D").stdout.splitlines()]
    if left:
        print(f"vcs: {len(left)} still staged as deleted after --apply: {left[:5]}")
        return 1
    print(f"vcs: reconciled {fixed} path(s); the index now agrees with HEAD")
    return 0

def cmd_blob_commit(base: str, branch: str, message: str, paths: List[str],
                    push: bool = False, repo: Optional[Path] = None,
                    allow_shrink: bool = False,
                    src: Optional[Path] = None,
                    advance: bool = False,
                    allow_stale: bool = False) -> int:
    rc, _sha = _blob_commit(base, branch, message, paths, push=push, repo=repo,
                            allow_shrink=allow_shrink, src=src,
                            advance=advance, allow_stale=allow_stale)
    return rc


def _blob_commit(base: str, branch: str, message: str, paths: List[str],
                 push: bool = False, repo: Optional[Path] = None,
                 allow_shrink: bool = False,
                 src: Optional[Path] = None,
                 advance: bool = False,
                 allow_stale: bool = False) -> tuple:
    """Commit exactly ``paths`` onto ``base`` via a private temp index. Prints
    the commit sha and diffstat; never touches the shared index or the
    worktree.

    Content comes from the worktree, or from ``src`` — a staging directory
    holding the same relative paths. ``src`` is what makes the safe pattern
    complete: prepare the change in a clean copy (``git archive <base> | tar
    -x``), edit there, ship from there. Without it the final step was "copy
    your files over the shared tree", i.e. the unsafe act the private index
    exists to avoid.

    With ``src``, a path that is ABSENT is an error rather than a recorded
    deletion. A staging directory normally holds only the files being shipped,
    so treating absence as deletion would turn a partial copy into silent
    removals — the exact shape of the sweep this tool's guards were written
    for. Deletions stay expressible through the worktree form.
    """
    root_r = _git(repo, "rev-parse", "--show-toplevel")
    if root_r.returncode != 0:
        return _die2("not inside a git repository")
    root = Path(root_r.stdout.strip())

    base_r = _git(root, "rev-parse", "--verify", f"{base}^{{commit}}")
    if base_r.returncode != 0:
        return _die2(f"base ref does not resolve: {base}")
    base_sha = base_r.stdout.strip()

    name, email = _identity(root)
    if not name or not email:
        return _die2("git identity unset — the commit would be anonymous")

    with tempfile.NamedTemporaryFile(prefix="awgit-blob-index-",
                                     delete=False) as tf:
        index = tf.name
    env = {"GIT_INDEX_FILE": index}
    try:
        r = _git(root, "read-tree", base_sha, env=env)
        if r.returncode != 0:
            return _die2(f"read-tree failed: {r.stderr.strip()[:200]}")
        for p in paths:
            fp = (src / p) if src else (root / p)
            if not fp.exists():
                if src:
                    return _die2(
                        f"{p} is not in the staging dir ({src}). With --from, "
                        f"a missing file is refused rather than committed as a "
                        f"deletion: a staging dir usually holds only the files "
                        f"you are shipping, so 'I copied three of five' would "
                        f"silently delete two. Express a deletion from the "
                        f"worktree form instead")
                r = _git(root, "update-index", "--force-remove", p, env=env)
                if r.returncode != 0:
                    return _die2(f"could not record deletion of {p}")
                print(f"vcs:   - {p} (deleted)")
                continue
            # STALE-BASE GUARD (--from only). If the base's newest commit for
            # this path is NEWER than the staged copy, that copy predates it and
            # cannot contain it -- so committing it REVERTS whatever landed in
            # between.
            #
            # The sweep guard below is blind to this by construction: it refuses
            # a copy that DELETES lines, and a revert deletes nothing. Measured
            # 2026-08-23 -- `class Awdk` went back to `class AitherAdk` one PR
            # after being fixed, while the two other edits in that same commit
            # survived, because the staging copy was taken before the rename
            # merged.
            if src and not allow_stale:
                lr = _git(root, "log", "-1", "--format=%ct", base_sha, "--", p)
                if lr.returncode == 0 and lr.stdout.strip().isdigit():
                    try:
                        staged_mt = int(fp.stat().st_mtime)
                    except OSError:
                        staged_mt = None
                    base_ct = int(lr.stdout.strip())
                    if staged_mt is not None and base_ct > staged_mt:
                        return _die2(
                            f"{p}: the staged copy is OLDER than the base's "
                            f"newest commit for it (by {base_ct - staged_mt}s), "
                            f"so it cannot contain that change and committing it "
                            f"would REVERT it. Re-copy from {base} and re-apply "
                            f"your edit, or pass --allow-stale if going backwards "
                            f"is genuinely the point.")

            hr = _git(root, "hash-object", "-w", "--path", p, str(fp))
            if hr.returncode != 0:
                return _die2(f"hash-object failed for {p}: "
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
                    base_blob = br.stdout.strip()
                    # Line-ending reconciliation before the count. A file
                    # whose STORED blob is CRLF (committed outside autocrlf,
                    # or before it was enabled) diffs as fully-rewritten
                    # against ANY LF copy, so a +7/-1 edit reads as "DELETES
                    # the whole file" and the guard refuses it — measured
                    # 2026-08-26 on .github/workflows/blog-autopublish.yml
                    # (stored CRLF, staged LF: numstat 250/244 for a 7-line
                    # edit). Normalise the base to LF first; a REAL
                    # stale-copy shrink deletes the same lines after
                    # normalisation, so the protection the guard exists for
                    # is unaffected.
                    norm_blob = base_blob
                    # RAW bytes on purpose: _git runs text=True, and text
                    # mode applies universal-newline translation, which
                    # already turns every \r\n into \n — so a CRLF blob is
                    # indistinguishable from an LF one through _git. Detect
                    # on the raw bytes instead, and feed the normalised
                    # content via --stdin — a temp file races the AV scanner
                    # on Windows (WinError 32 on unlink, measured 2026-08-26).
                    cat = subprocess.run(
                        ["git", "cat-file", "blob", base_blob],
                        capture_output=True)
                    if cat.returncode == 0 and b"\r\n" in cat.stdout:
                        nh = subprocess.run(
                            ["git", "hash-object", "-w", "--stdin"],
                            input=cat.stdout.replace(b"\r\n", b"\n"),
                            capture_output=True)
                        if nh.returncode == 0:
                            norm_blob = nh.stdout.decode(
                                "utf-8", errors="replace").strip()
                    ds = _git(root, "diff", "--numstat", norm_blob,
                              blob).stdout.split()
                    if len(ds) >= 2 and ds[0].isdigit() and ds[1].isdigit():
                        add, rm = int(ds[0]), int(ds[1])
                        # THRESHOLD, TIGHTENED — it under-triggered twice in one
                        # day. The first version needed rm > add*3, so a push
                        # deleting 84 lines while adding 49 sailed through and
                        # reverted a peer's whole feature. A commit whose author
                        # believes they are ADDING has no business deleting
                        # dozens of lines it never mentions, whatever the ratio,
                        # so the ratio test is now an OR rather than an AND:
                        # any sizeable deletion stops and asks.
                        if rm >= 25 or (rm > 5 and rm > add):
                            return _die2(
                                f"{p}: your copy DELETES {rm} line(s) the base "
                                f"has (+{add}) — a stale worktree copy sweeps "
                                f"peers' work exactly like this. Refresh it "
                                f"(`awgit read {base} {p} --out {p}`) or pass "
                                f"--allow-shrink if the deletion IS the point")
            r = _git(root, "update-index", "--add",
                     "--cacheinfo", f"100644,{blob},{p}", env=env)
            if r.returncode != 0:
                return _die2(f"update-index failed for {p}")
            print(f"vcs:   + {p}")
        tree = _git(root, "write-tree", env=env).stdout.strip()
        cr = _git(root, "commit-tree", tree, "-p", base_sha, "-m", message,
                  env={"GIT_INDEX_FILE": index,
                       "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                       "GIT_COMMITTER_NAME": name,
                       "GIT_COMMITTER_EMAIL": email})
        if cr.returncode != 0:
            return _die2(f"commit-tree failed: {cr.stderr.strip()[:200]}")
        sha = cr.stdout.strip()
    finally:
        try:
            os.unlink(index)
        except OSError as e:
            # A leaked temp index is litter, not a defect — but say so rather
            # than swallow it, or a full disk debugging session starts blind.
            print(f"vcs: note — temp index not removed ({e})")

    # ORPHAN GUARD — the named --branch already carries a commit this one does
    # NOT contain, so shipping this would silently drop that work.
    #
    # Measured 2026-08-23: `--base HEAD` was run twice in this tree minutes apart.
    # Peers commit here every 2-5 minutes, so HEAD MOVED between the runs and the
    # two commits came out as SIBLINGS with different parents rather than a chain.
    # Each diffstat looked perfectly correct in isolation; pushing the second
    # would have shipped the port fixes and silently dropped a registry brick,
    # two new gates and a resolver change. The tell is only visible by asking
    # `merge-base --is-ancestor`, which nothing did.
    #
    # This is the sibling of the shrink guard above: that one catches a stale
    # WORKTREE sweeping peers, this catches a stale BASE sweeping yourself.
    # Warn always; refuse only when actually pushing, since a local commit is
    # still recoverable and a refusal that cannot be overridden gets worked
    # around rather than heeded.
    for _ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        _e = _git(root, "rev-parse", "--verify", f"{_ref}^{{commit}}")
        if _e.returncode != 0:
            continue
        _tip = _e.stdout.strip()
        if _git(root, "merge-base", "--is-ancestor", _tip, sha).returncode == 0:
            continue  # properly chained
        print(f"vcs: WARNING — {_ref} is at {_tip[:12]}, which this commit "
              f"({sha[:12]}) does NOT contain. They are SIBLINGS, not a chain: "
              f"pushing this would drop that commit's work. Re-run with "
              f"--base {_tip[:12]} to chain onto it.")
        if push and not allow_shrink:
            return _die2(
                f"refusing to push a commit that orphans {_ref} ({_tip[:12]}). "
                f"Re-run with --base {_tip[:12]}, or pass --allow-shrink if "
                f"replacing that branch tip IS the point.")

    stat = _git(root, "diff", "--stat", base_sha, sha).stdout.strip()
    print(stat or "vcs: (empty diff — the named files match the base)")
    print(f"vcs: commit {sha[:12]} on top of {base} — the shared index and "
          f"worktree were not touched")
    if advance:
        # Fast-forward the LOCAL branch and make the shared index agree, in that
        # order. Without the second half this is the step that manufactures a
        # phantom staged deletion: HEAD gains a path the index has no entry for,
        # git renders it `D `, and a peer's plain `git commit` then deletes a
        # file that belongs in the tree.
        #
        # Only ever a true fast-forward. Peers commit here every few minutes, so
        # the window closes often, and forcing the ref past someone's commit
        # would orphan it -- the loss this whole tool exists to prevent,
        # reintroduced at the last step. A refusal is cheap: the commit still
        # exists by sha.
        ref = f"refs/heads/{branch}"
        cur = _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}")
        if cur.returncode != 0:
            # The ref does not exist yet. CREATING it is not a force: there is
            # no commit to orphan, so the refusal above bought no safety and
            # cost the thing --advance was asked for. What it actually produced
            # was a DANGLING commit -- on no branch, in no reflog, one gc from
            # gone -- which the caller then had to attach by hand every single
            # time they started a branch. Measured 2026-08-24: both commits that
            # day dangled for exactly this reason.
            #
            # The empty old-value is what keeps the peer rule intact. It tells
            # git "this ref must NOT exist", so if a peer creates the branch
            # between the check and the write, git REFUSES rather than
            # clobbering them -- the same guarantee as the fast-forward arm
            # below, applied to creation instead of to movement.
            created = _git(root, "update-ref", ref, sha, "")
            if created.returncode != 0:
                print(f"vcs: --advance: could not create {ref} — a peer may "
                      f"have just created it. Your commit is safe as "
                      f"{sha[:12]}.")
            else:
                print(f"vcs: {branch} created -> {sha[:12]}")
        elif cur.stdout.strip() != base_sha:
            print(f"vcs: --advance REFUSED: {ref} is at "
                  f"{cur.stdout.strip()[:12]}, not the base {base_sha[:12]} -- "
                  f"a peer moved it. Your commit is safe as {sha[:12]}.")
        elif _git(root, "update-ref", ref, sha).returncode != 0:
            print(f"vcs: --advance: update-ref failed for {ref}")
        else:
            print(f"vcs: {branch} -> {sha[:12]}")
            done, skipped = _reconcile_index(root, base_sha, sha, paths)
            if done:
                print(f"vcs: index reconciled for {len(done)} path(s) -- no "
                      f"phantom staged deletions left behind")
            for pth, why in skipped:
                print(f"vcs: index NOT reconciled for {pth}: {why}")

    if push:
        # REMOTE GUARD — ask the LIVE remote, never the local tracking refs.
        # The orphan guard above reads refs/heads/{branch} and
        # refs/remotes/origin/{branch}, both of which are LOCAL: a peer can
        # push between our last fetch and this push and neither ref moves, so
        # both pass while the push is a non-fast-forward. Measured 2026-08-27:
        # --advance REFUSED (the local branch had moved) but --push still
        # pushed, and GitHub accepted the sibling commit as a FORCE on an
        # unprotected branch, dropping a peer's ~170-file chain from develop.
        # Unconditional on purpose -- --allow-shrink disarms the orphan-guard
        # refusal (it was passed for a legitimate .gitmodules shrink the very
        # day of that incident), and the push is the last irreversible step:
        # forcing a remote ref is a deliberate human act, done with plain git.
        rmt = _git(root, "ls-remote", "origin", f"refs/heads/{branch}")
        if rmt.returncode != 0:
            return _die2(f"could not query origin/{branch} before pushing: "
                         f"{rmt.stderr.strip()[:200]}")
        rmt_tip = rmt.stdout.split()[0] if rmt.stdout.strip() else ""
        # An empty remote tip (branch not on origin yet) is a CREATE, not a
        # force -- same rule as the --advance creation arm. Otherwise the push
        # is only a fast-forward if the remote tip is an ancestor of our
        # commit; if it cannot be proven (missing objects included), refuse.
        if rmt_tip and _git(root, "merge-base", "--is-ancestor",
                            rmt_tip, sha).returncode != 0:
            return _die2(
                f"push REFUSED: origin/{branch} is at {rmt_tip[:12]}, not the "
                f"base {base_sha[:12]} -- a peer moved it, and pushing "
                f"{sha[:12]} would be a non-fast-forward (a FORCE on an "
                f"unprotected branch), orphaning their commit. Fetch and "
                f"re-run with --base {rmt_tip[:12]}. Your commit is safe as "
                f"{sha[:12]}.")
        pr = _git(root, "push", "origin", f"{sha}:refs/heads/{branch}")
        if pr.returncode != 0:
            return _die2(f"push failed: {pr.stderr.strip()[:200]}")
        print(f"vcs: pushed refs/heads/{branch}")
    else:
        # Say plainly that NOTHING references this commit. It was built with
        # commit-tree, so there is no reflog entry either: `git log <branch>`
        # cannot see it, `git branch -a --contains` is empty, and gc will
        # eventually take it. Printing only a push hint reads as an ordinary
        # successful commit, and that silence is what loses the work.
        contained = _git(root, "branch", "--contains", sha).stdout.strip()
        if not contained:
            print(f"vcs: WARNING {sha[:12]} is on NO branch and in no reflog -- "
                  f"attach it or it is one gc from gone:")
            if branch:
                print(f"vcs:   git branch -f {branch} {sha[:12]}"
                      f"   (or re-run with --advance)")
            else:
                print(f"vcs:   git branch -f <name> {sha[:12]}")
        if branch:
            print(f"vcs: push with  git push origin {sha[:12]}:refs/heads/{branch}")
    return 0, sha


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

    # EXPAND A DIRECTORY INTO THE REF'S FILE LIST FIRST.
    #
    # A directory argument resolves to a TREE. `hash-object` on a directory
    # yields nothing comparable, numstat comes back +0 -0, and the loop below
    # then printed the most reassuring line it has -- "looks like your edit,
    # not staleness" -- and exited 0, for a directory that did not exist here
    # at all.
    #
    # The per-file branch already handles absence correctly ("MISSING here but
    # present in <ref>"). It simply could not be REACHED, because you cannot
    # name a file you do not know is missing -- which is the whole reason
    # someone passes a directory instead of a file list.
    #
    # That gap matters most in exactly the situation this command exists for: a
    # checkout well behind the ref is missing whole FILES, not just lines, and a
    # build against it fails somewhere unrelated -- an import error in a file
    # that is itself perfectly up to date. Answering "your copy is fine" there
    # is worse than saying nothing.
    expanded: List[str] = []
    for pth in paths:
        kind = _git(root, "cat-file", "-t", f"{ref}:{pth}").stdout.strip()
        if kind != "tree":
            expanded.append(pth)
            continue
        listing = _git(root, "ls-tree", "-r", "--name-only", ref, "--", pth)
        members = [ln.strip() for ln in listing.stdout.splitlines() if ln.strip()]
        if not members:
            # A tree the ref has with nothing in it is not a state git can
            # produce; say so rather than silently judging zero files.
            print(f"vcs: {pth}: is a directory in {ref} with no files listed "
                  f"-- nothing judged")
            continue
        print(f"vcs: {pth}: directory -- checking {len(members)} file(s) "
              f"in {ref}")
        expanded.extend(members)
    paths = expanded

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
        # Same threshold as blob-commit's guard, and for the same reason:
        # the first version needed rm > add*3, so `fresh` called a copy 84
        # lines behind "your edit, not staleness" — reassuring the author
        # moments before they reverted a peer's whole feature. Both halves
        # must agree, or the pre-edit check keeps blessing exactly what the
        # commit-time guard would refuse.
        if rm >= 25 or (rm > 5 and rm > add):
            print(f"vcs: {pth}: your copy is BEHIND {ref} (+{add} -{rm}) — "
                  f"pushing it would sweep peers; refresh from {ref} first")
            behind += 1
        elif rm > 20:
            # MIXED: enough of your own additions to clear the ratio test, and
            # still a lot of the ref's lines missing. The ratio only separates a
            # PURE stale copy from an edit; it cannot see a copy that is both
            # edited AND behind, which is the ordinary state of a shared file
            # someone has been working in for a while.
            #
            # Measured 2026-08-23 on automation_backlog.yaml: +59 -90 printed
            # "looks like your edit, not staleness" while the worktree copy was
            # missing three rows peers had committed (143 entries against 146).
            # That is a positive claim this heuristic cannot support, and it is
            # reassurance in exactly the direction that loses other people's
            # work.
            #
            # Still exit 0 — it may genuinely be your edit, and failing here
            # would flag every large refactor. Say what is unresolved instead of
            # asserting the comfortable half.
            print(f"vcs: {pth}: differs from {ref} (+{add} -{rm}) — {rm} lines "
                  f"of {ref} are NOT in your copy. Large deletions are how a "
                  f"stale copy reverts peers: confirm they are yours before "
                  f"pushing this file.")
        else:
            print(f"vcs: {pth}: differs from {ref} (+{add} -{rm}) — looks "
                  f"like your edit, not staleness")
    return 1 if behind else 0


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def extract_rows(src: str, pat) -> dict:
    """key -> the row's FULL TEXT, single-line or multi-line block.

    A row is its key line plus every following line indented MORE than it. That
    one rule covers both ledger shapes we have:

      * a markdown table row (`| D-1 | ... |`) — the next line is another row at
        the same indent, so the block is exactly one line, as before;
      * a YAML block row (`  - signature: "x"` + indented `status:`/`reason:`) —
        the block is the whole decision.

    THIS IS NOT COSMETIC. The line-only version silently truncated a YAML row to
    its key line, so a union appended `- signature: "beta"` WITHOUT its status,
    target or reason: a row that reads as present and decides nothing. Verified
    2026-08-23 against automation_backlog.yaml's real shape.
    """
    lines = src.splitlines()
    out: dict = {}
    i = 0
    while i < len(lines):
        m = pat.match(lines[i])
        if not m:
            i += 1
            continue
        base = _indent_of(lines[i])
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if not nxt.strip():          # blank: keep only if the block resumes
                k = j
                while k < len(lines) and not lines[k].strip():
                    k += 1
                if k < len(lines) and _indent_of(lines[k]) > base and not pat.match(lines[k]):
                    j = k
                    continue
                break
            if pat.match(nxt) or _indent_of(nxt) <= base:
                break
            j += 1
        blk = lines[i:j]
        # Drop trailing BLANK LINES only. A bare .rstrip() would also eat
        # trailing whitespace on the last real line, and the caller locates the
        # spine's last row by exact string match to splice after it — a block
        # that no longer matches its own source silently appends nothing.
        while blk and not blk[-1].strip():
            blk.pop()
        out.setdefault(m.group(1), "\n".join(blk))
        i = j
    return out


#: Key patterns tried when the DEFAULT matches nothing on both sides.
#:
#: The default is a markdown table row, and the dispatch rule names TWO
#: ledgers this command resolves -- TECH_DEBT.md and the automation backlog.
#: The second is YAML, so the documented invocation
#: (`awgit union-rows <path>`, no flags) died on it with 'this file is not a
#: row ledger' -- for a file this function's own docstring says it was
#: measured against. extract_rows has understood YAML block rows all along;
#: only the key pattern was markdown-only.
#:
#: Tried ONLY when the primary finds nothing on EITHER side, so a real
#: markdown ledger can never be re-read under a YAML key, and the pattern
#: actually used is printed -- a union that silently changed what it
#: considers a row is worse than one that refuses.
_UNION_FALLBACK_PATTERNS = (
    (r"^\s*- signature:\s*[\"']?([^\"'\n]+?)[\"']?\s*$", "YAML `- signature:` rows"),
    (r"^\s*- id:\s*[\"']?([^\"'\n]+?)[\"']?\s*$", "YAML `- id:` rows"),
)


def cmd_union_rows(path: str, key_pattern: str = "", ref: str = "",
                   repo: Optional[Path] = None) -> int:
    """Resolve an append-only row ledger by ID-KEYED UNION.

    For a file where both sides mostly APPEND keyed rows (a debt ledger, a
    backlog, any markdown table with an id column), a textual merge conflicts
    on adjacency while the right answer is simply "keep every id once". The
    spine is the side holding MORE keyed rows — the lesson from doing this by
    hand: the side that looks like 'ours' is routinely the stale one — and the
    other side's unique rows are appended after the spine's last row. Rows are
    never dropped; dropping is a decision a human makes with an editor.

    WITH --ref, it works OUTSIDE a merge. That case is not exotic, it is the
    quiet one: two lineages each grow rows the other lacks and never conflict —
    no markers, no merge in progress, clean status. Measured 2026-08-23 on
    automation_backlog.yaml, where HEAD carried ~492 lines the worktree lacked
    while the worktree carried two DECIDED rows HEAD lacked. Neither side was a
    superset, so "keep the bigger one" discards decisions whichever way you pick,
    and the only thing that noticed was a downstream checker.

    In --ref mode the spine is ALWAYS the local file, never the larger side: rows
    that exist only locally are usually yours and unpushed, and a "sync" that
    deletes your own work is not sync.
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

    if ref:
        local_p = root / path
        if not local_p.is_file():
            return _die(f"no file at {local_p}")
        ours = local_p.read_text(encoding="utf-8")
        r = _git(root, "cat-file", "-p", f"{ref}:{path}")
        if r.returncode != 0:
            return _die(f"{ref}:{path} is unreadable — wrong ref or path")
        theirs = r.stdout
    else:
        ours, theirs = stage(2), stage(3)
        if ours is None or theirs is None:
            return _die(f"{path} is not in a two-sided merge conflict "
                        f"(run during a merge, on a conflicted path). To union "
                        f"against another lineage outside a merge, pass "
                        f"--ref <ref>.")

    def rows(src: str) -> dict:
        return extract_rows(src, pat)

    o_rows, t_rows = rows(ours), rows(theirs)
    if not o_rows and not t_rows and not key_pattern:
        # Only when the caller named NO pattern: an explicit one that matches
        # nothing is a typo to report, not a shape to guess at. Tried only
        # when the primary found nothing on EITHER side, so a real markdown
        # ledger can never be silently re-read under a YAML key.
        for _fpat, _fwhy in _UNION_FALLBACK_PATTERNS:
            _fp = _re.compile(_fpat)
            _fo, _ft = extract_rows(ours, _fp), extract_rows(theirs, _fp)
            if _fo or _ft:
                print("vcs: default (markdown) pattern found no rows; using "
                      + _fwhy)
                pat, o_rows, t_rows = _fp, _fo, _ft
                break
    if not o_rows and not t_rows:
        return _die(f"no rows in either side match {pat.pattern!r} — wrong "
                    f"--key-pattern, or this file is not a row ledger")
    if ref:
        # LOCAL IS ALWAYS THE SPINE HERE. Outside a merge the larger-side
        # heuristic is actively wrong: the ref can hold more rows while the
        # local file holds your unpushed ones, and taking the ref as spine
        # would rewrite your file and drop them.
        spine_src, spine_rows, other_rows, spine_name = (
            ours, o_rows, t_rows, "local")
    else:
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


def cmd_read(ref: str, path: str, out: str = "",
             repo: Optional[Path] = None) -> int:
    """Read a path at another ref, and REFUSE rather than return silence.

    The obvious command is ``git show <ref>:<path>``, and under MSYS/Git-Bash
    on Windows that argument is silently MANGLED by path conversion into
    ``ref\\path`` — git then resolves nothing and prints NOTHING. An empty read
    is indistinguishable from "that ref does not have this file", which is an
    ordinary answer, so the mangling does not look like a bug: it looks like
    information, and the wrong conclusion ("this is branch-local") gets drawn
    confidently. Measured twice in one session even with the trap written down.

    So this separates the three outcomes a raw read conflates:
      exit 0  the blob exists and its content follows (or is written to --out)
      exit 1  the ref resolves and does NOT contain that path — a real absence
      exit 2  the REF itself does not resolve — a typo or an unfetched remote,
              never to be read as absence
    """
    root_r = _git(repo, "rev-parse", "--show-toplevel")
    if root_r.returncode != 0:
        return _die("not inside a git repository")
    root = Path(root_r.stdout.strip())

    if _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}").returncode != 0:
        print(f"vcs: ref does not resolve: {ref} — fetch it, or fix the name. "
              f"This is NOT 'the file is absent there'")
        return 2

    # cat-file over the ref:path spelling, passed as ONE argv element by
    # subprocess (no shell), so no shell or MSYS layer can rewrite it.
    spec = f"{ref}:{path}"
    # BYTES throughout. Decoding here would fail on any file the console codec
    # cannot represent — a workflow with an emoji in a comment killed the first
    # version with UnicodeEncodeError on a cp1252 terminal, which is precisely
    # the "a read that fails looks like something else" class this command
    # exists to remove. Bytes also keep a CRLF file (or a PEM) byte-exact.
    br = subprocess.run(["git", "cat-file", "-p", spec], cwd=str(root),
                        capture_output=True)
    if br.returncode != 0:
        print(f"vcs: {ref} does not contain {path} (the ref resolves; the "
              f"path is genuinely absent there)")
        return 1
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(br.stdout)
        print(f"vcs: wrote {out} ({len(br.stdout)} bytes) from {ref}:{path}")
    else:
        try:
            sys.stdout.buffer.write(br.stdout)
            sys.stdout.buffer.flush()
        except (AttributeError, ValueError):  # a wrapped/captured stdout
            sys.stdout.write(br.stdout.decode("utf-8", "replace"))
    return 0


def cmd_port(shas: List[str], onto: str, message: str = "",
             branch: str = "", push: bool = False,
             paths: Optional[List[str]] = None,
             overwrite_diverged: bool = False,
             repo: Optional[Path] = None) -> int:
    """Port commits' FILE CONTENT onto another lineage, refusing where a
    cherry-pick would have raised a conflict.

    The branch you are on is usually not the one that ships, so "this fix
    exists on my branch and must now exist on the deploying branch" is a
    routine move — and `cherry-pick` is the wrong instrument when the two
    lineages have diverged by hundreds of commits: it either conflicts on
    adjacency that does not matter, or applies a diff whose context has
    shifted. This takes the SOURCE TIP's version of each touched path and
    writes it onto the base through a private index. No worktree, no checkout,
    no conflict — by construction.

    That construction has one hazard, and it is the whole safety story: if the
    BASE has changed a path since the source branched from it, writing the
    source's version REVERTS the base's change silently. That case is exactly
    where a cherry-pick would have stopped and asked, so this stops too:
    per-path, the base's blob is compared against the first sha's PARENT, and
    a path where they differ is REFUSED with both shas named. Pass
    ``--overwrite-diverged`` only once you have read what the base did — most
    often it already discharged your intent, and the right answer is to drop
    the path, not to transplant it.
    """
    root_r = _git(repo, "rev-parse", "--show-toplevel")
    if root_r.returncode != 0:
        return _die("not inside a git repository")
    root = Path(root_r.stdout.strip())
    if not shas:
        return _die("port needs at least one commit")

    base_r = _git(root, "rev-parse", "--verify", f"{onto}^{{commit}}")
    if base_r.returncode != 0:
        return _die(f"--onto does not resolve: {onto}")
    base_sha = base_r.stdout.strip()

    resolved = []
    for sh in shas:
        r = _git(root, "rev-parse", "--verify", f"{sh}^{{commit}}")
        if r.returncode != 0:
            return _die(f"commit does not resolve: {sh}")
        resolved.append(r.stdout.strip())

    # A MERGE commit has no single diff, so `diff-tree` prints nothing for it —
    # and reporting that as "touches no files" would be a silent no-op wearing
    # an ordinary answer. Name the real reason and point at the fix.
    merges = [sh for sh in resolved
              if len(_git(root, "rev-list", "--parents", "-n1",
                          sh).stdout.split()) > 2]
    if merges:
        return _die(
            f"{', '.join(m[:12] for m in merges)} is a MERGE commit — it has "
            f"no single diff to port. Name the commit that did the work (its "
            f"second parent's side), or pass --paths to say exactly which "
            f"files to carry from it")

    touched: List[str] = []
    for sh in resolved:
        r = _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", sh)
        if r.returncode != 0:
            return _die(f"could not read the paths {sh[:12]} touched")
        for line in r.stdout.splitlines():
            if line and line not in touched:
                touched.append(line)
    if paths:
        wanted = set(paths)
        missing = wanted - set(touched)
        if missing:
            return _die("these paths are not touched by the named commits: "
                        + " ".join(sorted(missing)))
        touched = [t for t in touched if t in wanted]
    if not touched:
        return _die("the named commits touch no files")

    tip = resolved[-1]
    first_parent = _git(root, "rev-parse", f"{resolved[0]}^").stdout.strip()

    # Divergence gate — the half that makes this safe rather than merely fast.
    diverged = []
    if not overwrite_diverged and first_parent:
        for t in touched:
            b = _git(root, "rev-parse", f"{base_sha}:{t}")
            o = _git(root, "rev-parse", f"{first_parent}:{t}")
            if b.returncode == 0 and o.returncode == 0 and \
                    b.stdout.strip() != o.stdout.strip():
                diverged.append(t)
    if diverged:
        print(f"vcs: REFUSING — {onto} has changed since these commits branched:")
        for t in diverged:
            bs = _git(root, "rev-parse", f"{base_sha}:{t}").stdout.strip()[:12]
            ps = _git(root, "rev-parse", f"{first_parent}:{t}").stdout.strip()[:12]
            print(f"vcs:   {t}  base={bs} source-parent={ps}")
        print(f"vcs: read what the base did first — most often it already "
              f"discharged your intent and the path should be DROPPED, not "
              f"transplanted. `awgit read {onto} <path>` shows it. Then either "
              f"re-run with --paths naming only the clean files, or "
              f"--overwrite-diverged once you have decided.")
        return 3

    name, email = _identity(root)
    if not name or not email:
        return _die("git identity unset — the commit would be anonymous")

    with tempfile.NamedTemporaryFile(prefix="awgit-port-index-",
                                     delete=False) as tf:
        index = tf.name
    env = {"GIT_INDEX_FILE": index}
    try:
        if _git(root, "read-tree", base_sha, env=env).returncode != 0:
            return _die("read-tree of the base failed")
        for t in touched:
            blob = _git(root, "rev-parse", f"{tip}:{t}")
            if blob.returncode != 0:
                if _git(root, "update-index", "--force-remove", t,
                        env=env).returncode != 0:
                    return _die(f"could not record deletion of {t}")
                print(f"vcs:   - {t} (deleted by the source)")
                continue
            mode = "100644"
            lsr = _git(root, "ls-tree", tip, "--", t).stdout.split()
            if lsr and lsr[0] in ("100644", "100755", "120000"):
                mode = lsr[0]
            if _git(root, "update-index", "--add", "--cacheinfo",
                    f"{mode},{blob.stdout.strip()},{t}",
                    env=env).returncode != 0:
                return _die(f"update-index failed for {t}")
            print(f"vcs:   + {t}")
        tree = _git(root, "write-tree", env=env).stdout.strip()
        msg = message or (f"port {' '.join(x[:12] for x in resolved)} onto "
                          f"{onto}")
        cr = _git(root, "commit-tree", tree, "-p", base_sha, "-m", msg,
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
            print(f"vcs: note — temp index not removed ({e})")

    print(_git(root, "diff", "--stat", base_sha, sha).stdout.strip()
          or "vcs: (empty diff — the base already has this content)")
    print(f"vcs: commit {sha[:12]} on top of {onto} — {len(touched)} path(s) "
          f"from {tip[:12]}, worktree untouched")
    if push:
        if not branch:
            return _die("--push needs --branch")
        # REMOTE GUARD — same rule as blob-commit's: the port commit's parent
        # is the resolved base, so pushing it is only a fast-forward while
        # origin/{branch} is an ancestor of it. Ask the LIVE remote: a peer
        # can push between the base resolution and this push, and a stale
        # local tracking ref would read as "still at the base". Refuse
        # unconditionally -- forcing a remote ref is a deliberate human act.
        rmt = _git(root, "ls-remote", "origin", f"refs/heads/{branch}")
        if rmt.returncode != 0:
            return _die(f"could not query origin/{branch} before pushing: "
                        f"{rmt.stderr.strip()[:200]}")
        rmt_tip = rmt.stdout.split()[0] if rmt.stdout.strip() else ""
        if rmt_tip and _git(root, "merge-base", "--is-ancestor",
                            rmt_tip, sha).returncode != 0:
            return _die(
                f"push REFUSED: origin/{branch} is at {rmt_tip[:12]}, not the "
                f"base {base_sha[:12]} -- a peer moved it, and pushing "
                f"{sha[:12]} would be a non-fast-forward (a FORCE on an "
                f"unprotected branch), orphaning their commit. Fetch and "
                f"re-run onto the new tip. Your commit is safe as {sha[:12]}.")
        pr = _git(root, "push", "origin", f"{sha}:refs/heads/{branch}")
        if pr.returncode != 0:
            return _die(f"push failed: {pr.stderr.strip()[:200]}")
        print(f"vcs: pushed refs/heads/{branch}")
    elif branch:
        print(f"vcs: push with  git push origin {sha[:12]}:refs/heads/{branch}")
    return 0


def _gh_repo(root: Path) -> str:
    """owner/name from origin, so nothing here hardcodes a repository."""
    url = _git(root, "remote", "get-url", "origin").stdout.strip()
    url = url.removesuffix(".git")
    if url.startswith("git@"):
        url = url.split(":", 1)[-1]
    parts = [x for x in url.split("/") if x]
    return "/".join(parts[-2:]) if len(parts) >= 2 else ""


def _gh_api(method: str, path: str, fields: Optional[dict] = None,
            root: Optional[Path] = None) -> tuple:
    """gh api with each -f as its OWN argv entry.

    Concatenating them into the path string ("pulls/7/merge -f m=squash") sends
    the whole thing as the URL and 404s — a PR that genuinely exists is reported
    Not Found, so the malformed call looks like a missing PR.
    """
    cmd = ["gh", "api", "-X", method, path]
    for k, v in (fields or {}).items():
        cmd += ["-f", f"{k}={v}"]
    r = subprocess.run(cmd, cwd=str(root) if root else None,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    import json as _json
    try:
        return r.returncode, _json.loads(r.stdout)
    except Exception:
        return r.returncode, None


def cmd_ship(base: str, branch: str, message: str, paths: List[str],
             title: str = "", body: str = "", merge: bool = False,
             delete_branch: bool = False, repo: Optional[Path] = None,
             allow_shrink: bool = False, src: Optional[Path] = None,
             allow_stale: bool = False) -> int:
    """commit -> push -> PR -> (optionally) merge, in one command.

    This chain was hand-run fifteen times in a single session before it had a
    name. Every step keeps the properties the individual commands earned: the
    commit goes through a private index (peers unswept), the shrink guard still
    refuses a stale copy, and the merge goes through the API rather than
    ``gh pr merge``.

    That last one matters more than it sounds. ``gh pr merge --delete-branch``
    runs a LOCAL ``git checkout``/``branch -d`` after the API merge succeeds,
    which fails in any repo with live worktrees ("'develop' is already used by
    worktree at ..."). gh then exits non-zero for a merge that ALREADY
    HAPPENED, so the failure reads as "the merge failed" and the branch is left
    behind on the remote. Here the merge and the branch delete are both plain
    API calls, and the result is VERIFIED by reading the PR's state back rather
    than trusting an exit code.
    """
    root_r = _git(repo, "rev-parse", "--show-toplevel")
    if root_r.returncode != 0:
        return _die("not inside a git repository")
    root = Path(root_r.stdout.strip())
    if not branch:
        return _die("ship needs --branch")
    gh_repo = _gh_repo(root)
    if not gh_repo:
        return _die("could not read owner/name from origin")

    # NO PATHS + --merge is MERGE-ONLY: land a branch already shipped. Without
    # this, re-running ship to land its own PR builds a second commit and the
    # push is rejected non-fast-forward — which is what happened the first time
    # ship shipped itself, and reads as "ship is broken" rather than "there is
    # nothing left to commit".
    url = ""
    base_branch = base.split("/", 1)[1] if base.startswith("origin/") else base
    if paths:
        rc, sha = _blob_commit(base, branch, message, paths, push=True,
                               repo=repo, allow_shrink=allow_shrink, src=src,
                               allow_stale=allow_stale)
        if rc != 0:
            return rc
        pr_title = title or (message.splitlines()[0] if message else branch)
        r = subprocess.run(
            ["gh", "pr", "create", "--repo", gh_repo, "--base", base_branch,
             "--head", branch, "--title", pr_title, "--body",
             body or pr_title],
            cwd=str(root), capture_output=True, text=True, encoding="utf-8",
            errors="replace")
        url = ((r.stdout or "").strip().splitlines() or [""])[-1]
        if r.returncode != 0 and "already exists" not in (r.stderr or ""):
            return _die(f"pr create failed: {r.stderr.strip()[:200]}")
    elif not merge:
        return _die("ship with no paths does nothing unless --merge (which "
                    "lands the PR already open for --branch)")
    if not url:
        vr = subprocess.run(["gh", "pr", "view", branch, "--repo", gh_repo,
                             "--json", "url", "--jq", ".url"], cwd=str(root),
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
        url = vr.stdout.strip()
    print(f"vcs: PR {url}")
    if not merge:
        print("vcs: not merging (pass --merge to land it)")
        return 0

    num = url.rstrip("/").split("/")[-1]
    if not num.isdigit():
        return _die(f"could not read a PR number from {url!r}")
    code, _ = _gh_api("PUT", f"repos/{gh_repo}/pulls/{num}/merge",
                      {"merge_method": "merge"}, root=root)
    # VERIFY, never trust the exit code — this is the whole point.
    vr = subprocess.run(["gh", "pr", "view", num, "--repo", gh_repo, "--json",
                         "state,mergedAt", "--jq", ".state"], cwd=str(root),
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace")
    state = vr.stdout.strip()
    if state != "MERGED":
        return _die(f"PR #{num} is {state or 'unknown'} after the merge call "
                    f"(api exit {code}) — read {url} before retrying")
    print(f"vcs: PR #{num} MERGED (verified by reading its state back)")
    if delete_branch:
        dcode, _ = _gh_api("DELETE", f"repos/{gh_repo}/git/refs/heads/{branch}",
                           root=root)
        print(f"vcs: remote branch {branch} "
              + ("deleted" if dcode == 0 else "NOT deleted (already gone?)"))
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
        # The shape that ESCAPED the first threshold: 84 deleted against 49
        # added is under rm > add*3, and it reverted a peer's whole feature.
        base_lines = [f"line {i}\n" for i in range(120)]
        (td / "ratio.txt").write_text("".join(base_lines), encoding="utf-8")
        _git(td, "add", "ratio.txt")
        _git(td, "commit", "-q", "-m", "ratio base")
        kept = base_lines[:36] + [f"new {i}\n" for i in range(49)]
        (td / "ratio.txt").write_text("".join(kept), encoding="utf-8")
        rc = cmd_blob_commit("HEAD", "x", "84 del / 49 add", ["ratio.txt"],
                             repo=td)
        assert rc == 1, "the 84-deleted/49-added shape was NOT refused"
        # ...and a genuinely small edit must still pass, or the guard floods.
        (td / "ratio.txt").write_text("".join(base_lines[:-2]) + "tail\n",
                                      encoding="utf-8")
        rc = cmd_blob_commit("HEAD", "x", "tiny edit", ["ratio.txt"], repo=td)
        assert rc == 0, "a 2-line edit was refused — the guard floods"
        rc = cmd_blob_commit("HEAD", "x", "shrink", ["big.txt"], repo=td,
                             allow_shrink=True)
        assert rc == 0, "--allow-shrink did not override"

        # CRLF base + LF staged copy: the stored blob is CRLF (committed
        # outside autocrlf) while hash-object --path normalises the staged
        # LF copy — so WITHOUT reconciliation a 2-line edit on a 40-line
        # CRLF base reads as "DELETES 40 lines" and the guard refuses it.
        # Measured 2026-08-26 on .github/workflows/blog-autopublish.yml
        # (numstat 250/244 for a +7/-1 edit). The reconciled guard must pass
        # the small edit…
        crlf_base = "".join(f"cline {i}\r\n" for i in range(40))
        (td / "crlf.txt").write_text("v1\n", encoding="utf-8")
        _git(td, "add", "crlf.txt")
        _git(td, "commit", "-q", "-m", "crlf placeholder")
        raw = (td / "raw-crlf.txt")
        raw.write_bytes(crlf_base.encode("utf-8"))
        crlf_blob = _git(td, "hash-object", "-w", str(raw)).stdout.strip()
        _git(td, "update-index", "--add", "--cacheinfo",
             f"100644,{crlf_blob},crlf.txt")
        _git(td, "commit", "-q", "-m", "crlf base")
        (td / "crlf.txt").write_text(
            "".join(f"cline {i}\n" for i in range(38)) + "tail\n",
            encoding="utf-8")
        rc = cmd_blob_commit("HEAD", "x", "crlf small edit", ["crlf.txt"],
                             repo=td)
        assert rc == 0, ("a 2-line edit on a CRLF base was refused — "
                         "line-ending false positive")
        # …and a REAL shrink of a CRLF base must still be refused.
        (td / "crlf.txt").write_text("stale\n", encoding="utf-8")
        rc = cmd_blob_commit("HEAD", "x", "crlf shrink", ["crlf.txt"], repo=td)
        assert rc == 1, "a CRLF-file shrink was NOT refused after reconciliation"


        # --from: content comes from a staging dir, and the SHARED WORKTREE IS
        # NOT READ. Proven by making the two disagree — the worktree copy says
        # "worktree", the staging copy says "staged", and the commit must
        # contain the staged one. A passthrough that quietly ignored src would
        # commit "worktree" and every other assertion here would still pass.
        staged = Path(tempfile.mkdtemp(prefix="awgit-staging-st-"))
        try:
            (staged / "sub").mkdir(parents=True, exist_ok=True)
            (td / "sub").mkdir(parents=True, exist_ok=True)
            (td / "sub" / "s.txt").write_text("worktree\n", encoding="utf-8")
            (staged / "sub" / "s.txt").write_text("staged\n", encoding="utf-8")
            # Deliberately NOT committed into the fixture, and the shared index
            # is not touched: these arms must not advance HEAD, or the LATER
            # arms — which measure a file against HEAD — start failing for a
            # reason unrelated to what they assert. That is exactly what
            # happened when this arm ran `add -A && commit`: the truncation arm
            # went red and named big.txt, which was innocent.
            # NOTE the sha: blob-commit builds a commit OBJECT and only prints
            # the push hint — it never creates a local branch, so `git show
            # wip/src:...` reads nothing. Read the object it actually made.
            rc, sha = _blob_commit("HEAD", "wip/src", "from staging",
                                   ["sub/s.txt"], repo=td, src=staged)
            assert rc == 0, "--from commit failed"
            got = _git(td, "show", f"{sha}:sub/s.txt").stdout
            assert got.strip() == "staged", (
                f"--from read the worktree, not the staging dir: {got!r}")

            # A path absent from the staging dir is REFUSED, never committed as
            # a deletion — otherwise a partial copy silently removes files.
            rc, _ = _blob_commit("HEAD", "wip/src2", "missing",
                                 ["sub/absent.txt"], repo=td, src=staged)
            assert rc != 0, (
                "a path missing from the staging dir was accepted — it would "
                "have been recorded as a deletion")
        finally:
            shutil.rmtree(staged, ignore_errors=True)

        # STALE-BASE guard, all three directions. A copy older than the base's
        # newest commit for that path is refused (committing it reverts), a
        # CURRENT copy is not (or the guard floods every honest --from), and
        # --allow-stale overrides for when going backwards is the point.
        import time as _time
        stale_dir = Path(tempfile.mkdtemp(prefix="awgit-stale-st-"))
        try:
            # The path must EXIST in the base with a commit, or there is
            # nothing to revert and the guard correctly stays quiet -- which
            # is all the first version of this arm actually proved.
            (td / "sub").mkdir(parents=True, exist_ok=True)
            (td / "sub" / "s.txt").write_text("base\n", encoding="utf-8")
            _git(td, "add", "sub/s.txt")
            _git(td, "commit", "-q", "-m", "base has this path")
            (stale_dir / "sub").mkdir(parents=True, exist_ok=True)
            sf = stale_dir / "sub" / "s.txt"
            sf.write_text("staged\n", encoding="utf-8")
            _old = int(_time.time()) - 86400
            os.utime(sf, (_old, _old))
            rc, _ = _blob_commit("HEAD", "wip/stale", "stale", ["sub/s.txt"],
                                 repo=td, src=stale_dir)
            assert rc != 0, ("a staged copy older than the base's commit for that "
                             "path was accepted -- that is how a revert ships")
            _now = int(_time.time())
            os.utime(sf, (_now, _now))
            rc, _ = _blob_commit("HEAD", "wip/fresh2", "fresh", ["sub/s.txt"],
                                 repo=td, src=stale_dir)
            assert rc == 0, "the guard fired on a CURRENT copy -- it would flood"
            os.utime(sf, (_old, _old))
            rc, _ = _blob_commit("HEAD", "wip/override2", "explicit",
                                 ["sub/s.txt"], repo=td, src=stale_dir,
                                 allow_stale=True)
            assert rc == 0, "--allow-stale did not override"
        finally:
            shutil.rmtree(stale_dir, ignore_errors=True)

        # ...and the worktree form still CAN express a deletion, or the guard
        # above has removed a capability rather than protected one.
        (td / "sub" / "s.txt").unlink()
        rc, _ = _blob_commit("HEAD", "wip/del", "delete it", ["sub/s.txt"],
                             repo=td)
        assert rc == 0, "the worktree form can no longer record a deletion"
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
        # the ratio shape `fresh` used to bless: 84 behind, 49 added.
        blines = [f"l{i}\n" for i in range(120)]
        (td / "fr.txt").write_text("".join(blines), encoding="utf-8")
        _git(td, "add", "fr.txt")
        _git(td, "commit", "-q", "-m", "fr base")
        (td / "fr.txt").write_text(
            "".join(blines[:36] + [f"n{i}\n" for i in range(49)]),
            encoding="utf-8")
        rc = cmd_fresh("HEAD", ["fr.txt"], repo=td)
        assert rc == 1, "fresh still blesses the 84-behind/49-added shape"

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
        # leave no merge in progress: the arms below create branches, and a
        # conflicted MERGE_HEAD makes `checkout -b` fail in a way that reads
        # as the new arm being broken.
        _git(td, "merge", "--abort")

        # union-rows --ref: the QUIET divergence, where nothing ever conflicted.
        # Two lineages each grew rows the other lacks; there is no merge, no
        # markers and a clean status, so the stage-2/3 path cannot see it at all.
        # The ref deliberately holds MORE rows than local, so the larger-side
        # heuristic would pick it as the spine. That must not happen: local
        # carries an EDIT to a shared row, and a ref spine silently reverts it.
        # (Row COUNT alone cannot catch this — both spines keep every row, which
        # is why an earlier version of this arm passed against the wrong rule.)
        (td / "ref_ledger.md").write_text(
            "| B-1 | base |\n| B-2 | pushed |\n| B-3 | pushed |\n",
            encoding="utf-8")
        _git(td, "add", "-A")
        _git(td, "commit", "-q", "-m", "ref ledger")
        _git(td, "tag", "upstream")
        (td / "ref_ledger.md").write_text(
            "| B-1 | EDITED LOCALLY |\n| B-9 | mine, unpushed |\n",
            encoding="utf-8")
        rc = cmd_union_rows("ref_ledger.md", key_pattern=r"^\| (B-\d+) \|",
                            ref="upstream", repo=td)
        assert rc == 0, "union --ref failed outside a merge"
        out = (td / "ref_ledger.md").read_text(encoding="utf-8")
        assert "B-9" in out, "--ref dropped the local unpushed row"
        assert "B-2" in out and "B-3" in out, "--ref did not bring in ref-only rows"
        assert out.count("B-1") == 1, "--ref duplicated the shared row"
        assert "EDITED LOCALLY" in out, (
            "--ref used the REF as spine and reverted a locally edited row. "
            "Outside a merge the local file is always the spine: the ref can "
            "legitimately hold more rows while local holds your unpushed work.")

        # union-rows on a BLOCK ledger: a row is its key line PLUS its indented
        # body. The line-only extractor appended a bare `- signature:` with no
        # status/target/reason — a row that reads as present and decides nothing.
        block_base = ('rows:\n'
                      '  - signature: "alpha"\n'
                      '    status: automated\n'
                      '    target: a.py\n')
        (td / "backlog.yaml").write_text(block_base, encoding="utf-8")
        _git(td, "add", "-A")
        _git(td, "commit", "-q", "-m", "backlog base")
        _git(td, "tag", "up2")
        (td / "backlog.yaml").write_text(
            block_base + '  - signature: "beta"\n    status: wontfix\n'
                         '    reason: one off\n', encoding="utf-8")
        _git(td, "commit", "-q", "-am", "local beta")
        _git(td, "tag", "local2")
        (td / "backlog.yaml").write_text(
            block_base + '  - signature: "gamma"\n    status: wontfix\n'
                         '    reason: theirs\n', encoding="utf-8")
        rc = cmd_union_rows("backlog.yaml",
                            key_pattern=r'^  - signature: (.+)',
                            ref="local2", repo=td)
        assert rc == 0, "union --ref failed on a block ledger"
        out = (td / "backlog.yaml").read_text(encoding="utf-8")
        assert '"beta"' in out and '"gamma"' in out, "a block row was dropped"
        assert "reason: one off" in out, (
            "the BODY of the appended block row was lost — this is the defect: "
            "a signature with no decision reads as decided and is not")
        assert out.count('signature: "alpha"') == 1, "spine block duplicated"

        # read: the three outcomes a raw `git show` conflates must differ.
        rc = cmd_read("HEAD", "ledger.md", repo=td)
        assert rc == 0, "existing path at a good ref did not read"
        rc = cmd_read("HEAD", "no-such-file.txt", repo=td)
        assert rc == 1, "absent path did not report absence"
        rc = cmd_read("no-such-ref", "ledger.md", repo=td)
        assert rc == 2, "bad ref reported as absence — the silent-mangle class"

        # port: a clean port lands; a path the BASE moved is REFUSED (exit 3)
        # — that is the case a cherry-pick would have raised as a conflict —
        # and --overwrite-diverged proceeds once a human has decided.
        _git(td, "checkout", "-q", "-b", "trunk")
        (td / "shipme.txt").write_text("base\n", encoding="utf-8")
        (td / "contended.txt").write_text("base\n", encoding="utf-8")
        _git(td, "add", "-A")
        _git(td, "commit", "-q", "-m", "trunk base")
        _git(td, "checkout", "-q", "-b", "feature")
        (td / "shipme.txt").write_text("the fix\n", encoding="utf-8")
        _git(td, "commit", "-q", "-am", "the fix")
        fix = _git(td, "rev-parse", "HEAD").stdout.strip()
        _git(td, "checkout", "-q", "trunk")
        rc = cmd_port([fix], "trunk", message="port clean", repo=td)
        assert rc == 0, "a clean port did not land"

        # now the base moves the SAME file the next source commit touches
        _git(td, "checkout", "-q", "feature")
        (td / "contended.txt").write_text("source edit\n", encoding="utf-8")
        _git(td, "commit", "-q", "-am", "source touches contended")
        src2 = _git(td, "rev-parse", "HEAD").stdout.strip()
        _git(td, "checkout", "-q", "trunk")
        (td / "contended.txt").write_text("BASE ALREADY FIXED IT\n",
                                          encoding="utf-8")
        _git(td, "commit", "-q", "-am", "base fixes contended")
        rc = cmd_port([src2], "trunk", message="should refuse", repo=td)
        assert rc == 3, "a diverged path was NOT refused"
        rc = cmd_port([src2], "trunk", message="decided", repo=td,
                      overwrite_diverged=True)
        assert rc == 0, "--overwrite-diverged did not proceed"

        # ship: the parts that can be proven WITHOUT a network. The repo
        # parser must survive every origin spelling (a wrong owner/name sends
        # the merge call to another repository), and _gh_api must place each
        # -f as its own argv entry — concatenating them into the path 404s a
        # PR that genuinely exists, so the malformed call reads as "no such
        # PR". Both are the kind of wrong that looks like an ordinary answer.
        for url, want in (
            ("https://github.com/o/n.git", "o/n"),
            ("https://github.com/o/n", "o/n"),
            ("git@github.com:o/n.git", "o/n"),
        ):
            _git(td, "remote", "remove", "origin")
            _git(td, "remote", "add", "origin", url)
            got = _gh_repo(td)
            assert got == want, f"origin {url} parsed as {got!r}, want {want!r}"
        _git(td, "remote", "remove", "origin")

        import subprocess as _sp
        seen = {}
        real_run = _sp.run

        def _spy(cmd, *a, **k):
            if isinstance(cmd, list) and cmd[:2] == ["gh", "api"]:
                seen["cmd"] = list(cmd)

                class _R:
                    returncode, stdout, stderr = 0, "{}", ""
                return _R()
            return real_run(cmd, *a, **k)

        _sp.run = _spy
        try:
            _gh_api("PUT", "repos/o/n/pulls/7/merge", {"merge_method": "merge"})
        finally:
            _sp.run = real_run
        assert seen["cmd"][-2:] == ["-f", "merge_method=merge"], \
            f"-f was not its own argv entry: {seen['cmd']}"
        assert "repos/o/n/pulls/7/merge" in seen["cmd"], "path was mangled"

        # ---- --advance, in its OWN repo -------------------------------
        # Deliberately not sharing the fixture above: these arms move the
        # branch and stage a file, and the first version of them broke the
        # union-rows arm further down -- which then failed naming neither.
        adv = Path(tempfile.mkdtemp(prefix="awgit-advance-st-"))
        bare = peer = None  # the remote-guard arm's repos; cleaned in finally
        try:
            _git(None, "init", "-q", "-b", "main", str(adv))
            _git(adv, "config", "user.name", "t")
            _git(adv, "config", "user.email", "t@example.invalid")
            (adv / "base.txt").write_text("b", encoding="utf-8")
            _git(adv, "add", "-A")
            _git(adv, "commit", "-q", "-m", "base")

            # The phantom. Before this flag existed, a NEW file was `??`
            # while the branch sat still and became `D ` + `??` the moment
            # the branch moved onto the commit -- so a peer running a plain
            # `git commit` deleted a file that belonged in the tree.
            (adv / "brand-new.txt").write_text("n1", encoding="utf-8")
            h0 = _git(adv, "rev-parse", "HEAD").stdout.strip()
            rc = cmd_blob_commit(h0, "main", "add brand-new",
                                 ["brand-new.txt"], repo=adv,
                                 advance=True)
            assert rc == 0, "--advance blob-commit failed"
            assert _git(adv, "rev-parse",
                        "refs/heads/main").stdout.strip() != h0, (
                "--advance did not move the branch")
            st = _git(adv, "status", "--porcelain", "--",
                      "brand-new.txt").stdout
            assert not st.startswith("D"), (
                "--advance left a PHANTOM staged deletion: " + repr(st))

            # A branch that does not exist yet must be CREATED, not refused.
            # Refusing produced a dangling commit -- on no branch and in no
            # reflog -- which is strictly worse than creating a ref nothing
            # else points at, and it made --advance useless for the ordinary
            # case of starting a branch.
            (adv / "on-new-branch.txt").write_text("nb", encoding="utf-8")
            h1 = _git(adv, "rev-parse", "HEAD").stdout.strip()
            rc = cmd_blob_commit(h1, "brand-new-branch", "start a branch",
                                 ["on-new-branch.txt"], repo=adv, advance=True)
            assert rc == 0, "--advance onto a new branch failed"
            made = _git(adv, "rev-parse", "--verify",
                        "refs/heads/brand-new-branch")
            assert made.returncode == 0, (
                "--advance did not CREATE the missing branch; the commit is "
                "dangling")
            assert made.stdout.strip() != h1, (
                "--advance created the branch at the base, not at the commit")

            # The create-race (peer creates the ref between our check and our
            # write) is NOT asserted here: it is not reachable single-threaded,
            # and an arm that cannot fail is worse than no arm. It is handled by
            # passing an empty old-value to update-ref, which makes git itself
            # refuse; the existing REFUSED arm below covers the peer-moved case.

            # The isolation half, which is the one that matters: a peer has
            # staged an edit to a path this commit also names. Reconciling
            # must LEAVE THEIR VERSION ALONE -- otherwise the repair for a
            # phantom becomes the sweep the private index exists to stop.
            (adv / "shared.txt").write_text("s1", encoding="utf-8")
            _git(adv, "add", "shared.txt")
            _git(adv, "commit", "-q", "-m", "shared base")
            (adv / "shared.txt").write_text("PEER-STAGED", encoding="utf-8")
            _git(adv, "add", "shared.txt")
            (adv / "shared.txt").write_text("MINE", encoding="utf-8")
            h1 = _git(adv, "rev-parse", "HEAD").stdout.strip()
            rc = cmd_blob_commit(h1, "main", "mine", ["shared.txt"],
                                 repo=adv, advance=True)
            assert rc == 0, "blob-commit with a peer-staged path failed"
            staged_now = _git(adv, "show", ":shared.txt").stdout
            assert staged_now == "PEER-STAGED", (
                "reconcile CLOBBERED a peer staged edit: "
                + repr(staged_now))

            # ...and it must REFUSE when the branch is not the base rather
            # than force the ref past whatever a peer just landed.
            (adv / "z.txt").write_text("z", encoding="utf-8")
            stale = _git(adv, "rev-parse", "HEAD~1").stdout.strip()
            before = _git(adv, "rev-parse",
                          "refs/heads/main").stdout.strip()
            cmd_blob_commit(stale, "main", "stale base", ["z.txt"],
                            repo=adv, advance=True, allow_shrink=True)
            after = _git(adv, "rev-parse",
                         "refs/heads/main").stdout.strip()
            assert before == after, (
                "--advance moved a branch that was NOT the base -- that "
                "orphans whatever the peer landed")

            # --push must REFUSE a non-fast-forward against the LIVE remote,
            # even when every local ref still passes. The advance arms above
            # move local refs only; the remote guard is what stops a force
            # landing on the branch everyone else pulls. 2026-08-27: --advance
            # REFUSED while --push still pushed a sibling onto develop,
            # orphaning a peer's ~170-file chain -- the local refs had moved,
            # the guard printed, and --allow-shrink (passed for a legitimate
            # .gitmodules shrink) disarmed the refusal. Asserted with a real
            # bare origin so the remote genuinely moves underneath us.
            (adv / "pushme.txt").write_text("p1", encoding="utf-8")
            push_base = _git(adv, "rev-parse", "HEAD").stdout.strip()
            bare = Path(tempfile.mkdtemp(prefix="awgit-push-st-"))
            _git(None, "init", "-q", "-b", "main", "--bare", str(bare))
            _git(adv, "remote", "add", "origin", str(bare))
            _git(adv, "push", "-q", "origin", "main")
            # a peer lands a sibling commit on the remote ref, direct
            peer = Path(tempfile.mkdtemp(prefix="awgit-peer-st-"))
            _git(None, "init", "-q", "-b", "main", str(peer))
            _git(peer, "config", "user.name", "peer")
            _git(peer, "config", "user.email", "peer@example.invalid")
            _git(peer, "remote", "add", "origin", str(bare))
            _git(peer, "fetch", "-q", "origin", "main")
            _git(peer, "checkout", "-q", "FETCH_HEAD")
            (peer / "peer.txt").write_text("peer", encoding="utf-8")
            _git(peer, "add", "-A")
            _git(peer, "commit", "-q", "-m", "peer lands on the remote")
            _git(peer, "push", "-q", "origin", "HEAD:main")
            rmt_tip = _git(None, "ls-remote", str(bare),
                           "refs/heads/main").stdout.split()[0]
            rc = cmd_blob_commit(push_base, "main", "push onto stale",
                                 ["pushme.txt"], repo=adv, push=True)
            assert rc != 0, "--push FORCED the remote ref past a peer commit"
            assert _git(None, "ls-remote", str(bare),
                        "refs/heads/main").stdout.split()[0] == rmt_tip, (
                "--push moved the remote ref despite refusing")
            # with the remote back AT the base, the same push must succeed
            # and move it -- a genuine fast-forward is the ordinary case
            _git(bare, "update-ref", "refs/heads/main", push_base, rmt_tip)
            rc = cmd_blob_commit(push_base, "main", "push onto base",
                                 ["pushme.txt"], repo=adv, push=True)
            assert rc == 0, "--push refused a genuine fast-forward"
            assert _git(None, "ls-remote", str(bare),
                        "refs/heads/main").stdout.split()[0] != push_base, (
                "--push did not move the remote ref on a fast-forward")

            # reconcile-index repairs a phantom made the old way, and
            # reports rather than acts unless asked.
            (adv / "late.txt").write_text("L", encoding="utf-8")
            h2 = _git(adv, "rev-parse", "HEAD").stdout.strip()
            _rc, late = _blob_commit(h2, "main", "late", ["late.txt"],
                                     repo=adv)
            _git(adv, "update-ref", "refs/heads/main", late)
            dirty = _git(adv, "status", "--porcelain", "--",
                         "late.txt").stdout
            assert dirty.startswith("D"), (
                "the phantom did not reproduce, so the repair arm below "
                "would prove nothing: " + repr(dirty))
            assert cmd_reconcile_index(repo=adv) == 0
            assert _git(adv, "status", "--porcelain", "--",
                        "late.txt").stdout.startswith("D"), (
                "reconcile-index changed the index WITHOUT --apply")
            assert cmd_reconcile_index(repo=adv, apply=True) == 0
            fixed = _git(adv, "status", "--porcelain", "--",
                         "late.txt").stdout
            assert not fixed.startswith("D"), (
                "reconcile-index --apply left the phantom: " + repr(fixed))
        finally:
            shutil.rmtree(adv, ignore_errors=True)
            if bare:
                shutil.rmtree(bare, ignore_errors=True)
            if peer:
                shutil.rmtree(peer, ignore_errors=True)
        tail = "" if found else " (named-file arm: dangling commit unseen)"
        print("selftest: isolation, sweep guard, fresh, union-rows, read,"
              " port and ship"
              " behaved" + tail)
        return 0
    finally:
        shutil.rmtree(td, ignore_errors=True)
