# Changelog

All notable changes to `awgit` are recorded here.

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
