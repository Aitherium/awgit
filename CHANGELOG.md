# Changelog

All notable changes to `awgit` are recorded here.

## [1.10.0] — 2026-08-23

### Fixed — `union-rows` silently TRUNCATED a multi-line row to its first line

A row was whatever the key pattern matched on ONE line. That is right for a
markdown table (`| D-1 | ... |`) and wrong for every block-structured ledger:
`automation_backlog.yaml` rows are `  - signature: "x"` plus an indented
`status:`/`target:`/`reason:` body, and the union appended only the signature
line. The result is a row that reads as present and DECIDES NOTHING — worse than
a dropped row, because every downstream scan counts it as handled.

A row is now its key line plus every following line indented more than it, which
covers both shapes with one rule: a table row's next line is at the same indent,
so its block is still exactly one line. Behaviour for markdown ledgers is
unchanged.

### Added — `union-rows --ref`, for the divergence that never conflicted

`union-rows` required a two-sided merge conflict. That is the LOUD case. The
quiet one is two lineages that each grew rows the other lacks and never
conflicted at all: no markers, no merge in progress, clean status — and the
command answered "not in a two-sided merge conflict", which reads as "there is
nothing wrong here".

Measured on a real ledger the same day: one side carried ~492 lines the other
lacked, while that other side carried two DECIDED rows the first lacked. Neither
was a superset, so "keep the bigger one" discards decisions whichever way you
pick, and only a downstream checker noticed.

With `--ref`, LOCAL IS ALWAYS THE SPINE — never the larger side. Outside a merge
the ref can legitimately hold more rows while your file holds unpushed work, and
a sync that reverts your own edits is not a sync. Rows only the ref has are
appended; rows only you have are left exactly as they are.

Both are pinned by mutation-verified self-test arms. The spine arm deliberately
asserts that a LOCALLY EDITED shared row survives, because row COUNT cannot tell
the two spines apart — an earlier version of that arm passed against the wrong
rule, which is the failure mode a self-test is most likely to have.

## [1.9.0] — 2026-08-23

### Fixed — blob-commit left PHANTOM staged deletions behind

`blob-commit` builds through a private index and never touches the shared one,
which is the entire point and which is correct. It stops being sufficient the
moment the LOCAL branch moves onto the commit: HEAD then holds a path the index
has no entry for, git renders that as `D ` — a staged DELETION — beside a `??`
for the same path, and **a peer running a plain `git commit` deletes a file that
belongs in the tree.**

Reproduced from a clean repo: `?? new-file.txt` while the branch sits still,
`D  new-file.txt` + `??` the instant it moves. Measured in a live shared tree
the day this was written: five such phantoms, from several sessions, sitting
among genuine staged deletions and indistinguishable from them in `git status`.

### Added — `blob-commit --advance`

Commits, fast-forwards the local branch, and reconciles the shared index for
exactly the paths committed. It is the whole operation in one step, and it never
leaves a phantom behind.

Two refusals are load-bearing:

* it advances **only** a true fast-forward. Peers commit every few minutes, so
  the window closes often, and forcing a ref past someone's commit orphans it —
  the loss this tool exists to prevent, reintroduced at the last step. Refusing
  is cheap: the commit still exists by sha.
* a path is reconciled **only** when the index still holds exactly what the base
  held, i.e. nobody has staged anything for it. Any other index state is a peer
  mid-edit and is left alone and reported. Without that check the repair for a
  phantom would become the sweep the private index exists to stop.

### Added — `awgit reconcile-index`

Finds, and with `--apply` repairs, phantoms already on disk: a path the index
calls deleted while the file is present and byte-identical to HEAD. Reports by
default, because a deliberate `git rm --cached` on a file left on disk looks
identical from the outside and that is the caller's decision, not the tool's.

Four selftest arms, mutation-verified in both directions: dropping the reconcile
call reproduces `PHANTOM staged deletion: 'D  brand-new.txt
?? brand-new.txt'`,
and removing the peer-staged guard reproduces `CLOBBERED a peer staged edit`.
They run in their own temp repo — the first version shared the existing fixture,
left it on another branch, and broke the `union-rows` arm further down, which
then failed naming neither.

## [1.8.0] — 2026-08-23

### Fixed — the sweep guard under-triggered, twice in one day

`blob-commit`'s shrink guard required `rm > add * 3`, so a push deleting 84
lines while adding 49 sailed straight through and reverted a peer's entire
feature — the second time that class landed in a single session, and the first
time the guard built to stop it was the thing that let it past. `fresh`
carried the identical threshold, so the PRE-edit check had already blessed the
same copy as "your edit, not staleness" moments earlier.

Both now refuse at `rm >= 25 or (rm > 5 and rm > add)`. A commit whose author
believes they are ADDING has no business deleting dozens of lines it never
mentions, whatever the ratio — so the ratio test became an OR rather than an
AND. Two selftest arms pin it: the exact 84-deleted/49-added shape that
escaped is refused, and a 2-line edit still passes, because a guard that
floods gets switched off rather than satisfied.

## [1.7.0] — 2026-08-23

### Added

- **`awgit ship --base <ref> --branch <name> -m "..." <paths> [--merge]`** —
  commit, push, open the PR and (optionally) land it, in one command. This
  chain was hand-run fifteen times in a single session before it had a name,
  and every step keeps what the individual commands earned: the commit goes
  through a private index so peers are unswept, and the shrink guard still
  refuses a stale copy.

  The merge goes through the **API**, not `gh pr merge`, and that is not a
  preference. `gh pr merge --delete-branch` runs a LOCAL `git checkout` /
  `branch -d` after the API merge has already succeeded, which fails in any
  repo with live worktrees (`'develop' is already used by worktree at ...`) —
  so gh exits non-zero for a merge that HAPPENED, the failure reads as "the
  merge failed", and the remote branch is left behind. Here the merge and the
  branch delete are plain API calls, and the outcome is **verified by reading
  the PR's state back** rather than trusting an exit code.

  `--delete-branch` removes the remote branch only after that verification.

## [1.6.0] — 2026-08-23

### Added

- **`awgit port <sha>... --onto <ref>`** — port commits' file CONTENT onto
  another lineage. The branch you are on is usually not the one that ships,
  and `cherry-pick` is the wrong instrument once the two lineages have
  diverged by hundreds of commits: it conflicts on adjacency that does not
  matter, or applies a diff whose context has moved. This writes the source
  tip's version of each touched path onto the base through a private index —
  no worktree, no checkout, no conflict by construction.

  That construction has exactly one hazard, and handling it is the whole
  point: if the BASE changed a path since the source branched, writing the
  source's version REVERTS the base silently. So per path the base's blob is
  compared against the first commit's PARENT, and a divergent path is
  **REFUSED (exit 3)** with both shas named — the case where a cherry-pick
  would have stopped and asked. Proven live on its first real invocation: it
  refused a port that would have reverted two files develop had since fixed.
  `--overwrite-diverged` proceeds once a human has read what the base did;
  most often the base already discharged the intent and the right answer is to
  DROP the path, not transplant it. `--paths` carries a subset.

  A MERGE commit is refused by name rather than reported as "touches no
  files" — `diff-tree` prints nothing for a merge, and letting that read as
  "nothing to port" would be a silent no-op wearing an ordinary answer.

## [1.5.0] — 2026-08-23

### Added

- **`awgit read <ref> <path>`** — read a path at another ref and REFUSE rather
  than return silence. `git show <ref>:<path>` is silently MANGLED by MSYS
  path conversion on Windows Git-Bash (`ref\path`), git resolves nothing and
  prints NOTHING — and an empty read is indistinguishable from "that ref does
  not have this file". The mangling therefore does not look like a bug, it
  looks like information, and "this must be branch-local" gets concluded
  confidently. Three exits separate what one conflated: **0** content (bytes,
  so a CRLF file or a PEM stays byte-exact and an emoji cannot kill the read
  on a cp1252 console), **1** the ref resolves and the path is genuinely
  absent, **2** the REF does not resolve — never to be read as absence.
  `--out FILE` writes the bytes instead of printing them.

## [1.4.0] — 2026-08-23

### Added — surgery in a shared worktree, and the guards it earned

Four commands for the case this repo lives in every day: a checkout several
sessions commit to concurrently, where the ordinary git moves (rebase, `git
add -A`, a pathspec commit against a moved branch) silently revert a peer's
in-flight work.

- **`awgit scratch <dest>`** — a partial clone (`--filter=blob:none`) of
  origin with your git identity configured locally, so plain `git commit`
  works there. The safe home for a large merge is a checkout of your own;
  every session that made one re-typed the same filter and identity flags.
  Idempotent: re-running against an existing clone fetches and re-asserts.

- **`awgit blob-commit --base <ref> -m "..." <paths...>`** — commit EXACTLY
  the named worktree files onto a base ref through a private temporary index.
  The shared index and the worktree are never touched, so concurrent sessions
  cannot sweep you and you cannot sweep them. `--push` sends it; without it
  you get the sha and the push line.

- **`awgit fresh <ref> <paths...>`** — is my copy BEHIND that ref? Run it
  before editing a file peers also move. Exit 1 when a path's worktree copy
  deletes far more than it adds against the ref, which is what a stale
  checkout looks like from the inside.

- **`awgit union-rows <path>`** — resolve a conflicted append-only row ledger
  (a debt ledger, a backlog, any id-keyed markdown table) by keeping every id
  exactly once. The spine is the side with MORE rows, because the side that
  feels like "ours" is routinely the stale one. Nothing is ever dropped.

### The guard, and why it is there

`blob-commit` REFUSES a named file whose worktree copy shrinks sharply
against the base (`--allow-shrink` overrides). This is not a hypothetical:
the tool's own first production push carried a file 2,335 lines behind the
base and silently reverted 137 rows of other people's work, caught one commit
later in a diffstat. The guard turns that class into a refusal before the
commit object exists, and `fresh` is the same question asked earlier, before
the edit rather than before the push.

`awgit blob-commit --selftest` proves all of it offline in a temporary repo:
isolation (a peer's staged edit must not leak into the commit, and the shared
index must be undisturbed after), the shrink refusal AND its override, the
`fresh` verdicts in both directions, and a union that keeps every id once.

## [1.1.2] — 2026-08-19

### Fixed
- **Zombie worktrees are now detected and named at the source.** `git worktree
  remove` is not atomic: it unregisters the worktree (deletes its `.git`
  pointer, drops the admin entry) and THEN deletes the working directory's
  contents — if step two fails partway (a locked file, a permission error on
  Windows), git reports the failure but the worktree is already unregistered
  and pointerless. What's left is a zombie: any `git` command run inside it
  later silently resolves to the PARENT repo instead, with no warning. This is
  exactly how a `git reset --hard` believed to be scoped to an isolated
  worktree once ran against a shared main repo. `remove()` now detects this
  shape and returns a loud, distinct "PARTIAL REMOVAL — ZOMBIE" message with
  the two-step recovery (`git worktree prune`, then delete the directory by
  path) instead of reporting an ordinary failure. Covered by
  `tests/test_worktree_zombie_detection.py`, mutation-tested.

## [1.1.1] — 2026-08-14

### Fixed
- **`stage-mine` wrote CRLF blobs on Windows.** The merged content was fed to
  `git hash-object` through subprocess text mode, which translates `
` to
  `os.linesep` on stdin — so every line reached git as CRLF while HEAD and the
  lease baseline were LF. A 51-line edit staged as a full-file line-ending
  rewrite, while the command's own accounting still said "51 added / 0 removed"
  (`splitlines()` cannot see line endings). The blob is now written as bytes,
  through `--path` so the repository's clean filters (gitattributes /
  autocrlf) apply exactly as `git add` would. The self-test gains a raw-bytes
  assertion on the staged blob — every other read in it is text-mode, which
  normalizes CRLF back on read and structurally cannot see this class.

## [1.1.0] — 2026-08-13

### Row-level diff for tabular data

A line diff is useless on a table: sort it and every line "changed"; reorder two
rows and a review drowns. So a row now gets the same pair a function gets — an
identity and a content address:

    row identity = H(the --key columns)   which row is this?
    row content  = H(every column)        has it changed?

The diff is set algebra on identity, so rows can be reordered freely and nothing
is reported, and a changed cell reads as MODIFIED rather than an unrelated add
plus remove.

```bash
awgit data diff old.csv new.csv --key id
awgit data diff old.csv new.csv --key id --json    # before/after per row
```

`data` is a new verb, not an overload — `awgit diff` still means node diff.

Three failure modes are refused rather than guessed:

- **no `--key`** degrades to a content set-diff and says so; MODIFIED stays empty
  rather than being inferred.
- **a key column present in neither table** is an error. Keying on a missing
  column would report every row as added *and* removed, which looks exactly like
  data loss.
- **an unreadable or headerless file** raises and exits 2, never a successful
  diff of zero rows.

CSV and TSV need nothing beyond the stdlib. Parquet needs the new optional
extra: `pip install awgit[tabular]`.

Adapted from [Oxen](https://github.com/oxen-ai/Oxen) (Apache-2.0); no code is
vendored. awgit's byte encoding deliberately differs — it length-prefixes each
field, so two different rows cannot share a content address — which means hashes
are not comparable between the two tools.

## [1.0.0] — 2026-08-12

awgit stops being a sidecar you remember to invoke and becomes the tool you
drive: stacked commits, one pull request per commit, review threads that survive
the code moving, and evidence attached to the change itself.

The 1.0 is a commitment to the command surface below. Everything in it is
exercised by the test suite (ACC008 holds that at zero), documented (ACC007
holds that at 40/40), and free of internal references (AWP001 holds that at
zero). `push` was verified against real GitHub: three commits became three pull
requests, each showing one file, bases chained, and two amends plus a restack
updated them in place instead of opening duplicates.

Stacked commits, and `push` as the way a pull request is opened. `awgit` stops
being a sidecar you remember to invoke and becomes the tool you drive.

### Added
- **Stacked commits.** `stack`/`sl`, `prev`, `next`. Work is the commits between
  trunk and HEAD, each a reviewable change.
- **`push` IS the pull request.** One PR per commit, based on the commit below
  it, so a reviewer reads one logical change instead of a 900-line branch.
  There is no `pr create`. Pushing after `commit --amend` adds a REVISION to the
  same PR rather than opening a second one.
- **`Awgit-Change-Id`**, a trailer written once by a `prepare-commit-msg` hook.
  A sha identifies a snapshot; this identifies the CHANGE, and survives amend,
  rebase and cherry-pick — which is what makes the line above true.
- **`absorb`**, routing pending edits into the commits that own them BY NODE
  rather than by line blame, so the answer survives reindentation, renaming and
  moving between files.
- **A rewrite guard.** `absorb`/`uncommit`/`restack`/`pull` refuse when another
  actor holds a lease in the same worktree and point at `awgit worktree new`.
  A solo checkout has no other actors and rewrites freely.
- **`worktree new|list|rm`**, **`pr list|view|checks|merge|wait`**
  (`wait` exits 0 on the condition and 124 on timeout), **`commit`** (lease
  checked, captured), **`init`**, **`version`**.
- **`commands --json`** — the whole CLI as data, introspected from the live
  parser, so an agent never scrapes `--help`. Every read-only command takes
  `--json`.
- **Git passthrough** for ~20 everyday verbs plus `awgit git -- <args>`.
  No existing verb was repurposed: `awgit diff` still means a node-level diff.

- **Review threads anchored to a NODE**, not a line. `review show|comment|
  submit|resolve`; drafts until submit; the line is COMPUTED each time, so a
  thread follows the function down the file, survives a rebase and a reformat,
  and is re-found when it moves to another file (ambiguity is refused, not
  guessed). A thread whose node was deleted shows as `[orphaned]` with its text
  intact. Unresolved threads block `pr merge`.
- **`prove` — proof-carrying review.** The nodes a change touched and what each
  gate actually returned, with VIOLATION (exit 1) and DEAD (exit 2) kept apart.
  No gates at all is exit 2: "nothing verified this" must not be spelled the
  same way as "verified". `--markdown` renders a PR comment. Gate runners attach
  through `awgit.plugins`.
- **`owners` — ownership measured, not declared.** CODEOWNERS says who should
  review; the op-log knows who actually changed those nodes, under a verified
  identity, recency-weighted so ownership decays. Both are shown, because the
  disagreement is the signal.
- **`code def|search`**, answering from the node registry, and saying plainly
  that an uncaptured symbol is ABSENT rather than missing.
- **`clone` / `sparse`** — instant checkout at any size via git's own partial
  clone plus a cone sparse-checkout. `sparse status` reads the REPOSITORY, not
  the flag: `git clone --filter` succeeds and warns when a server declines, so
  awgit reports lazy only when the filter and a promisor remote both really
  exist.
- **`queue` / `ci`**, driving GitHub's merge queue and Actions rather than
  reimplementing either.

### Fixed
- **An unparseable file was recorded as "every node deleted".** A file carrying
  live conflict markers parses to zero nodes, which diffs as deletion — a
  confidently wrong record in a log meant to be authoritative. Unknown is now
  distinct from empty.
- **BOM-prefixed files were silently skipped**, because the parser read plain
  utf-8 and `ast` rejects the resulting U+FEFF.
- **`register_tools()` returned 5 and registered nothing** — five MCP tools
  reported as wired, none of them wired.
- **`awgit init` did not exist** despite being step one of the documented setup.
- **`restack` did not restack.** Amending a commit in the middle of a stack
  orphans everything above it, and `restack` rebased onto trunk instead of
  repairing them — so `prev` + amend + `restack` printed "HEAD is up to date"
  and silently DROPPED the rest of the stack. It now replays orphans, matched
  by Change-Id and scoped to commits whose parent was REPLACED, so deliberately
  discarded work is never resurrected.
- **`sync export` / `sync import` did not exist**; the documented spelling for
  the package's headline feature errored. Both now work.

## [0.3.1] — 2026-08-10

`awgit ledger` was unusable from its own output.

### Fixed
- **`--op` refused the identifier the listing printed.** The listing rendered
  `ledger_ref` as column 1 and the op_id NOWHERE, while `--op` matched only
  `op_id`. So copying an id off `awgit ledger` and passing it back answered
  `vcs: ledger: no ops match` — which reads as "that op does not exist", not as
  "you passed the wrong one of two ids you were never shown". There was no way
  to look an op up from the command that lists ops.
- `--op` now takes **either** id, by **prefix**. The prefix part is load-bearing
  and was learned the hard way: the first fix printed the op_id too, abbreviated
  to 16 chars, while still matching on equality — reintroducing the identical
  defect one layer down, on the id it had just added. Ambiguous prefixes fail
  loudly with the match count rather than silently answering about the wrong op.
- The listing now prints the abbreviated op_id alongside `ledger_ref`.

### Added
- **`awgit ledger --json`** — the full `EditOp` set, not a re-parse of a display
  string that was never a contract. This is the machine seam for anything
  programmatic (world-model seeding, reward programs, exports).

### Notes
- The in-repo upstream copy receives the lookup fix
  and the printed op_id; `--json` is standalone-package-only for now, because
  upstream's `_cmd_ledger` also carries the ACTA `--credit` path and the two
  functions have deliberately diverged. AWG005's parity checks (`_actor`,
  `coverage_gap`, `is_guarded`) are unaffected.
- AWG006 compares **version strings only** (`local == published`), so it can see
  an unreleased *bump* but not unreleased *source drift*. This bump is what makes
  the fix visible to it — until `0.3.1` is published, AWG006 correctly goes red.

## [0.3.0] — 2026-08-09

awgit stops being Python-only, and starts drawing itself.

### Added
- **Multi-language node identity.** `awgit` understood 7,824 files in the repo
  it was built for and was blind to the other 12,600 — every `.ts`, `.tsx`,
  `.go`, `.cs` file was invisible to the diff, the merge engine and the graph,
  because node identity came from CPython's `ast`. It now borrows the parser
  the surrounding platform already runs: **75 extensions across 20+ languages**,
  via the optional `awgit[multilang]` extra.

  The property that made this possible, verified before the adapter was
  written: those symbol ids are **stable under movement**. A TypeScript
  function that changed position *and* had its body rewritten kept its id.
  Stability under movement is the whole basis of node-level merge.

  Python still parses natively — its node ids are already in existing op-logs,
  and switching would orphan them. The dependency is optional by design: awgit
  imports and guards Python without it, a file the parser chokes on degrades to
  "no symbols" rather than losing the commit's op, and a file type nobody can
  parse is skipped rather than recorded as an empty change.

- **`awgit graph`** — the op-log is a graph, so it renders as one. `mermaid`
  for humans (files as subgraphs, code nodes inside, a node two or more actors
  touched drawn as a collision) and node/edge `json` so it can be ingested
  alongside other graphs instead of being a private format. An empty op-log
  says so explicitly, because a blank diagram reads as "nothing is wrong"
  rather than "no data".

### Fixed
- **Capture attributed every agent to one identity.** `resolve_actor` resolved
  to the verified GitHub login, and where many agents share one login that
  collapsed every session into a single actor — making "two actors touched this
  node" inexpressible and the collision view structurally blind. Drawing the
  graph is what exposed it: 188 ops across 189 files, one actor. The session
  now supplies the *claimed* actor while `verified_actor` still records the
  verified identity — the schema always separated those, capture just wasn't
  using the distinction. This also aligns capture with the lease gate, which
  had already moved to per-session actors; the two disagreeing meant leases
  discriminated per session while captures did not.

## [0.2.0] — 2026-08-09

The release that makes the concurrent-edit guard actually guard something.

0.1.0 shipped a lease registry and a pre-commit gate whose entire purpose is
stopping two agents from clobbering each other's work — and in practice it
prevented nothing, because it could not be switched on without breaking the
machine it ran on. This release fixes that, and widens what it protects.

### Added
- **`awgit lease acquire --staged`** — take leases on exactly the staged files
  the gate will check. Complying with enforcement is now one command instead of
  a per-file chore, which is what makes broader coverage survivable.
- **Automatic per-session identity.** `_actor()` derives `claude:<session-id>`
  from `CLAUDE_CODE_SESSION_ID` (stable for a session and inherited by the git
  hook's subprocess), then falls back to `user@host`. Nothing to export.

### Changed
- **The lease gate no longer covers only `.py`.** `coverage_gap()` now consults
  `is_guarded()`: source (`.py .ts .tsx .js .jsx .mjs`), config
  (`.yml .yaml .toml .json .ini .cfg`), scripts (`.sh .bash .ps1 .psm1`), `.md`,
  and `.sql .proto .env` — minus bulk content nobody races on (site content,
  generated data, published artifacts). Not "everything": guarding bulk content
  would make a routine commit need dozens of leases, and a gate that heavy gets
  routed around rather than satisfied.

### Fixed
- **Enforcement could not be enabled.** `_actor()` returned `"unknown"` unless
  `AITHER_ACTOR` was exported, and `lease-check` rejects an unknown actor — so
  `VCS_LEASES_ENFORCE=1` would have blocked *every* commit until every session
  remembered a variable. A guard that can only be turned on by breaking the
  machine never gets turned on, and it never was.
- **A `.yml` commit reported `lease-check OK` without checking anything**, so
  compose files, CI workflows and docs were exactly as clobberable as before.

### Why this matters, measured
Across 568 commits in one week on the repo this was built for: the median commit
touches **1** guarded file, 89% touch five or fewer, and **9%** touch a file that
another commit touched inside a five-minute window — the collision git resolves
by letting both through and telling you later. During the same period a single
file was overwritten by a concurrent session **four times**, the last overwrite
silently reverting a fix that was already committed upstream.

## [0.1.0] — 2026-08-08

Initial public release: semantic (node-level) diff and merge for Python, an
edit-op log, a content-addressed body store, a lease registry, and the git hook
chain that drives capture and the pre-commit gate.
