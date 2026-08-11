# Changelog

All notable changes to `awgit`. Dates are the GitHub release dates for
[Aitherium/awgit](https://github.com/Aitherium/awgit/releases); versions match
what is published on [PyPI](https://pypi.org/project/awgit/).

This file exists because `sync_awgit_public.py` has always listed
`CHANGELOG.md` in `SYNC_PATHS` and the file had never been written. Its mirror
loop skips a missing source (`if not src.exists(): continue`), so every publish
quietly shipped no changelog and reported success — the exact fail-soft class
`check_publisher_source_on_default.py` (PUB001) now catches.

## 0.4.0 — 2026-08-11

*the hooks guard the tree, not just the commit*

- **`pre-push` is now chained.** `awgit hooks install` wraps it the same way it
  wraps `pre-commit`: the existing body moves to `pre-push.org` and is sourced
  first, then `.d/` fragments run. This matters more here than anywhere else —
  a repo's `pre-push` may already refuse pushes to the wrong remote, and a hook
  that overwrote it would silently delete that guard while looking like an
  upgrade.
- **`vcs-mass-delete-guard`** (pre-commit): refuses a commit that deletes more
  than `AITHER_MASS_DELETE_LIMIT` tracked files (default 50).
  `AITHER_ALLOW_MASS_DELETE=1` overrides.

  It exists because `git sparse-checkout` fires **no hook at all** — measured,
  with a probe at `post-checkout`: zero firings on both `set` and `disable`. So
  nothing can prevent a sparse-checkout aimed at the wrong directory, an
  `rm -rf`, or a script with an unset path variable. What they share is where
  the damage becomes permanent — the commit — which is the last point that is
  both hook-reachable and reversible. A real incident left a working tree with
  28,490 staged deletions; this blocks that commit.
- **`ci-gate-parity`** (pre-push): runs a repo's static CI gate set before the
  push, blocking only on gates that were passing in your checkout and now fail.
  Opt-out with `AITHER_SKIP_GATES=1`.
- **`AGENTS.md`** ships with the package, so an agent can bootstrap awgit and
  its hooks without reading the monorepo it came from.

## 0.3.1 — 2026-08-11

- `awgit ledger` was unusable from its own output: the command printed entries
  in a form its own parser would not accept.

## 0.3.0 — 2026-08-10

*beyond Python, and it draws itself*

- **Multi-language node identity.** Symbols are resolved through `repowise`
  for 75 file extensions and 20+ languages, so a TypeScript or Go function
  gets a stable node id the same way a Python one does. Previously anything
  non-Python was invisible to the op-log.
- **`awgit graph`** — renders the op-log as mermaid (files as subgraphs, code
  nodes inside, a node two authors touched drawn as a collision) or as
  node/edge JSON for a graph store.
- **`awgit evidence`** — reports what the op-log actually measured, splitting
  *confirmed* multi-agent collisions from ambiguous ones. A single worker
  appearing under an old and a new attribution label looks like a collision
  and is not; only the confirmed count is a claim.
- **`awgit lease acquire --staged`** — claim exactly what is about to be
  committed.
- Actor identity is derived per session (`CLAUDE_CODE_SESSION_ID`), so leases
  need no configuration.

## 0.2.0 — 2026-08-10

*the guard actually guards*

- **Lease enforcement works.** With `VCS_LEASES_ENFORCE=1` the pre-commit hook
  refuses a commit whose staged files the session does not hold, with
  `no active lease covering: <path>`. Before this the check computed a verdict
  and never acted on it.
- **Coverage is by guarded suffix, not `.py`.** `coverage_gap()` had asked
  whether a path ended in `.py`, so every other language was reported as
  fully covered by construction.
- Capture records whether an edit was actually leased, instead of a hardcoded
  `leased=False` that made adoption unmeasurable.

## 0.1.0

- Initial standalone package extracted from the AitherOS monorepo: stable
  code-node ids, the edit-op log, the lease registry, and the chained git
  hooks (`awgit hooks install`) that never overwrite an existing hook.
