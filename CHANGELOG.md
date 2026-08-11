# Changelog

All notable changes to `awgit` are recorded here.

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
- The in-repo upstream copy (`AitherOS/lib/awgit/cli.py`) receives the lookup fix
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
