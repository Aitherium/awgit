# awgit for agents

Read this if you are an agent (or a human) working in a repository that has
`.awgit/`. It is short on purpose: three commands do almost everything.

## Why this exists

Many agents edit one repository at once. Git records a tidy linear history and
tells you nothing about the case that actually hurts: **two sessions editing the
same function minutes apart**. Git sees two commits to one file and merges them
cleanly. awgit keys every edit to a stable **code-node id**, so that collision
is a thing you can see — and, if you want, refuse.

## Bootstrap (once per checkout)

```bash
pip install awgit
awgit init                 # creates .awgit/, picks up the repo
awgit hooks install        # CHAINS the git hooks — never overwrites them
```

`hooks install` is safe to re-run and safe to undo (`awgit hooks uninstall`). It
does **not** replace an existing hook: your current `pre-commit` body moves to
`pre-commit.org` and is sourced first, then `.d/` fragments run. This matters —
a repo's `pre-push` may already refuse pushes to the wrong remote, and a hook
that overwrote it would silently delete that guard while looking like an
upgrade.

What gets installed:

| hook | fragment | what it does |
|---|---|---|
| `pre-commit` | `vcs-lease-check` | refuses a commit touching a file you do not hold a lease on (only when enforcement is on) |
| `post-commit` | `vcs-capture` | records the edit-ops for the commit — this is what builds the graph |
| `pre-push` | `ci-gate-parity` | runs the repo's static CI gate set before the push, blocking on **regressions only** |

## The three commands you will actually use

```bash
awgit lease acquire --staged     # claim what you are about to commit
awgit lease list                 # who else is in this file right now
awgit graph                      # mermaid: files, nodes, who touched what
```

Leases are per-session and derived automatically (`CLAUDE_CODE_SESSION_ID` when
present), so there is nothing to configure. With enforcement on
(`VCS_LEASES_ENFORCE=1`), a commit whose staged files you do not hold is
refused with `no active lease covering: <path>`.

## Rules that keep this useful

- **Acquire before you edit, not before you commit.** A lease taken at commit
  time records that you were there; a lease taken before the edit is what
  stops the second agent walking in.
- **Never `--no-verify` to get past the lease check.** That is the one bypass
  that makes the whole record wrong: the op-log then describes a history that
  did not happen. If the check is wrong, fix the check.
- **A gate is not a suggestion.** `pre-push` blocks on gates that were passing
  in your checkout and now fail — a regression *you* caused. It deliberately
  does not block on what was already red, because a hook that blocks every
  push gets bypassed, and a bypassed hook is worse than none: it also teaches
  the bypass.
- **A green hook is not a green build.** The hook runs *your* interpreter. If
  CI pins an older Python, a version-gated defect passes locally and fails
  there. Measured 2026-08-10 in the AitherOS monorepo: three separate checker
  defects were invisible on 3.12 and only failed on CI's 3.10.

## What the evidence is for

`awgit evidence` and `aither-manifest.json` publish what the op-log actually
measured — ops captured, distinct actors, **confirmed multi-agent collisions**,
lease adoption. The number that means something is the confirmed collision
count: one code node edited by two *distinct* sessions. The raw count is
higher, because a single worker appearing under an old and a new attribution
label looks like a collision and is not.

Absent beats stale: if the evidence cannot be gathered, the manifest is not
written and the generator exits non-zero, rather than publishing a number that
outlives the thing it measured.
