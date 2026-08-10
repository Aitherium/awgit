# Changelog

All notable changes to `awgit` are recorded here.

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
