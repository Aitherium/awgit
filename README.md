# awgit — the git that knows a function from a line

Git has no world model. It knows which lines moved, not what a function is — so
"which commit owns this edit", "did this change conflict with mine", and "is
this the same change I pushed before" are all answered by heuristics over text.

**awgit rides on top of git** (git stays the byte-truth, the transport, the
history) and adds the layer above it: a **world model as the database**,
**semantic edit-ops as transactions**, **stacked commits with one pull request
each**, **verified identity**, and **leases** so several agents can work in one
tree without overwriting each other.

```bash
pip install awgit
awgit init            # verify the actor, install the capture hooks
```

Needs Python 3.10+ and `git`. `gh` (the GitHub CLI) is needed for `push` and
`pr`, and for the verified identity — everything else works without it.

## The model in one screen

```bash
awgit stack                  # your commits above trunk — one PR each
awgit commit -m "fix parser" # git commit, lease-checked and captured
awgit push                   # one pull request per commit (dry run; --apply)
awgit absorb                 # route pending edits into the commits they belong to
awgit pr wait 42 --for merged
```

There is no `awgit pr create`. **Push is how a pull request is opened**, and
pushing again after `commit --amend` adds a REVISION to the same PR rather than
opening a second one.

## Stacked commits

Work is a stack of commits between trunk and HEAD, each one a reviewable change
that becomes its own pull request, diffed against the commit below it. A
reviewer reads one logical change instead of a 900-line branch, and the bottom
of a stack can land while the top is still being argued about.

| | |
|---|---|
| `awgit stack` / `awgit sl` | the stack, newest first, with each commit's Change-Id |
| `awgit prev` / `awgit next` | move down / up the stack |
| `awgit push` | publish it — one PR per commit |
| `awgit absorb` | fold pending edits into the commits that own them |
| `awgit uncommit` | undo the last commit, keep its changes |
| `awgit restack` / `awgit pull` | rebase onto trunk (`pull` fetches first) |
| `awgit worktree new` (also `list`, `rm`) | a checkout of your own |

### Identity that survives a rewrite

Every commit carries an `Awgit-Change-Id` trailer, written once by a
`prepare-commit-msg` hook. A sha identifies a *snapshot*; the Change-Id
identifies the *change*, and it survives `--amend`, `rebase` and `cherry-pick` —
which is what lets `push` find the pull request you already opened instead of
opening another one.

```bash
awgit change-id show          # this commit's change id
awgit change-id find I3f2a…   # every commit carrying it
```

### absorb routes by node, not by line

git answers "which commit owns this change" per LINE, so reindenting a function
makes every line blame the reformat. awgit asks which commit last changed **this
node**, so the answer survives reindentation and line movement, and a rename
within a file. (A function moved to a *different* file gets a new node id —
capture's rename detection is per-file — so absorb sees a delete and an add.)

```bash
awgit absorb                 # show the routing
awgit absorb --apply         # create the fixups and autosquash
```

Generated and minified files are skipped and reported — they have no node
identity, and one bundle can stall a parser indefinitely.

## Review that survives the code moving

```bash
awgit review show                                   # threads on this change
awgit review comment --node <id> -b "needs a bound" # a DRAFT
awgit review submit                                 # publish them all at once
awgit review resolve <thread>
```

A comment is anchored to a **node**, not to a line. The line number is computed
each time from where the node is now, so a thread follows the function down the
file as code is inserted above it, survives a rebase and a reformat, and is
re-found when the function moves to another file. A thread whose node was
DELETED is shown as `[orphaned]` with its text intact — "the function you
objected to is gone" is a review outcome, not a reason to hide the objection.

Comments are drafts until `submit`, so you can read a whole diff and publish
once. **Unresolved threads block `awgit pr merge`**: "merge and follow up" is
how an objection becomes a TODO nobody files.

Node ids come from capture, so comment on a node the diff showed you
(`awgit diff <a> <b> --json`).

## Big repositories

```bash
awgit clone <url> <dest> --paths AitherOS/lib   # history now, contents on demand
awgit sparse add apps/web                       # materialise more
awgit sparse status                             # is this clone ACTUALLY lazy?
```

`awgit clone` uses git's own partial clone (`--filter=blob:none`) with a cone
sparse-checkout: history and trees arrive immediately, file content is fetched
the first time something reads it, and the working tree holds only the
directories you asked for.

This is deliberately not a virtual filesystem. A VFS means FUSE on Linux and
ProjFS on Windows — and Microsoft, who wrote both ProjFS and VFSforGit, retired
that approach in favour of exactly this pair. Reimplementing what its own author
walked away from would trade a supported git feature for a kernel-adjacent
dependency on the platform where it is least reliable.

**`sparse status` reads the repository, not the flag you passed.**
`git clone --filter` succeeds and warns when a server declines to filter — you
get a full clone and exit 0. awgit reports lazy only when the filter is really
in the config and a promisor remote really exists to backfill from.

## Landing it: evidence, ownership, the queue

```bash
awgit prove                  # what this change touched, and what checked it
awgit owners <path>          # who owns it — declared AND measured
awgit code def <symbol>      # where it is defined
awgit queue enqueue 42       # GitHub's merge queue
awgit ci status
```

**`awgit prove` is proof-carrying review.** A pull request normally arrives as a
diff and an assertion; "all checks passed" and "no checks ran" render almost
identically. `prove` attaches the nodes the change touched and what each gate
actually returned — and it never reports success without evidence: a gate that
exits 1 is a violation, a gate that exits 2 *could not judge*, and no gates at
all is **not proved** (exit 2), because "nothing verified this" must not be
spelled the same way as "verified". `--markdown` renders it as a PR comment.

Your gate runner attaches through the plugin seam, so awgit never has to know
what your checks are:

```python
from awgit import plugins
plugins.register(plugins.GATES, lambda paths: [{"name": "ruff", "status": "ok"}])
```

**`awgit owners` shows both answers.** CODEOWNERS says who *should* review;
the op-log knows who actually changed those nodes, weighted so ownership decays
(someone who last touched a module a year ago is not who you want reviewing it).
When the two disagree, that is the useful part — usually the file is stale.

`awgit queue` drives **GitHub's** merge queue rather than reimplementing one: it
already rebases, re-tests and lands, and a second implementation would be a
service that disagrees with it.

## Working next to other people (and other agents)

A lease says "I am editing this". Take one **before** you edit: the baseline it
captures is what lets `stage-mine` separate your work from a peer's.

```bash
awgit lease list                    # who holds what right now
awgit lease acquire <paths>         # claim files, TTL-renewed
awgit stage-mine <path>             # stage only YOUR edits to a shared file
```

History rewrites (`absorb`, `uncommit`, `restack`, `pull`) refuse when another
actor holds a lease in the same worktree, and point you at `awgit worktree new`.
A solo checkout has no other actors and rewrites freely; set
`AWGIT_ALLOW_UNSAFE_REWRITE=1` to override.

## The world model

Every commit is captured as an `EditOp`: which functions/classes/methods
changed, their old and new bodies by content address, the actor, the parent
chain. Ops key on **stable node ids** that survive renames and moves.

```bash
awgit status                  # op-log status
awgit diff <sha> <sha>        # NODE-level diff (not a text diff — see below)
awgit graph --format mermaid  # the op-log as a graph
awgit ledger --sha <sha>      # who changed what, under a verified identity
awgit evidence                # the measurable claim, from your own op-log
awgit bodies --get <sha>      # read a body from the content-addressed store
awgit dedupe --scan <trees>   # quantify duplication; --reclaim to hard-link
```

- **Merge** at node granularity: disjoint node sets merge clean by
  construction, and a genuine collision escalates naming the exact function
  (`awgit merge-preview`, `awgit merge-conflicts`, `awgit resolve-conflict`).
- **Differential sync**: `awgit sync export` emits the ops a peer is missing
  plus the bodies they reference; `awgit sync import` applies idempotently.

## It speaks git

Everyday verbs forward to git untouched — `awgit log`, `awgit add`,
`awgit show`, `awgit rebase`, and about twenty more — and `awgit git -- <args>`
runs anything else.

**No verb is repurposed.** `awgit diff` has always meant a node-level diff and
still does; git's own diff is `awgit git diff`. `awgit status` reports the
op-log. Silently changing what an existing command returns would break callers
in ways that surface far from the change.

## For agents

```bash
awgit commands --json    # the entire CLI as JSON: every command, flag and type
```

Read it once instead of scraping `--help`. It is introspected from the live
parser, so a command cannot be described unless it exists. Every read-only
command takes `--json`.

`awgit pr wait <n> --for merged` exits **0** when the condition holds and
**124** on timeout, so a loop can tell "it happened" from "I gave up".

## Set up

```bash
awgit init                # verify the actor, install hooks, report the store
awgit hooks install       # chained hooks — preserves any you already have
                          #   post-commit  -> `awgit capture` (records the op)
                          #   pre-commit   -> `awgit lease-check` (the lease gate)
                          #   prepare-commit-msg -> stamps the Change-Id
awgit hooks uninstall     # restore them
awgit version --json
```

The durable store lives **outside** the git tree at `~/.aither/awgit/data`
(override with `VCS_DATA_ROOT`), so the op-log and body store never pollute your
repository or your clones.

## Tests

```bash
cd awgit && python -m pytest tests/ -q
```

## License

Apache-2.0. Built by Aitherium — the git that scales to agents.

---

<!-- aither-ecosystem:start GENERATED from the ecosystem registry. Edits here are overwritten; change the registry instead. -->

## The aw family

Standalone tools that share one idea: **replace something you would otherwise have to _trust_ with something you can _check_.**

Each installs on its own, works offline, and needs no account.

| | instead of trusting | you check |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | a framework's idea of how your agents should run | one loop you can read, pointed at a backend you already pay for |
| [awskills](https://github.com/Aitherium/awskills) | that an agent knows your procedure | the procedure written down, versioned, and loadable by any agent |
| [awm](https://github.com/Aitherium/awm) | that memory stayed in its lane | tenant:user:project scopes, so a write cannot cross a boundary |
| [awnode](https://github.com/Aitherium/awnode) | a vendor's cloud with every prompt | a local gateway routing to backends you chose |
| [awgraph](https://github.com/Aitherium/awgraph) | that grep found everything | an AST + tree-sitter call graph an agent can traverse |
| **awgit** _(you are here)_ | that no one else is editing this file | a lease, refused at commit time if you do not hold it |
| [awseal](https://github.com/Aitherium/awseal) | that the artifact came from who you think | an Ed25519 seal — the key that verifies is not the key that forges |
| [awshare](https://github.com/Aitherium/awshare) | that the download is intact | content-addressed bundles, verified on fetch |
| [awnest](https://github.com/Aitherium/awnest) | that there is a person on the other end | a verdict with evidence, where "we could not tell" is not "yes" |
| [awnboard](https://github.com/Aitherium/awnboard) | a share link anyone who sees it can use | an invitation addressed to one person, for one gate, revocable |
| [awnix](https://github.com/Aitherium/awnix) | that the box is what you left it as | an immutable image you built, with atomic rollback |
| [awrecover](https://github.com/Aitherium/awrecover) | that the restore worked | a restore that fully lands or does not land at all |
| [awrelay](https://github.com/Aitherium/awrelay) | a SaaS in the middle of your agents | findings, alerts and coordination over your own transport |
| [awmail](https://github.com/Aitherium/awmail) | a mailbox somebody else can read | mail your agents send and receive over your own server |
| [awfind](https://github.com/Aitherium/awfind) | one vendor's idea of the web | results from whichever providers you configured |
| [awbrowse](https://github.com/Aitherium/awbrowse) | that the page said what you were told | the render, the DOM and the requests it made |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | a vendor's quantisation defaults | sub-byte KV cache kernels you can benchmark yourself |
| [AitherZero](https://github.com/Aitherium/AitherZero) | a pile of scripts nobody has numbered | numbered, discoverable automation with declarative playbooks |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | what a page tells your browser to do | a federated search and desktop bridge you host |

[**awnix**](https://github.com/Aitherium/awnix) is the ground floor — A Linux you can hand to an agent — immutable base, capabilities included.

## The Aitherium ecosystem

Every repository here is public. Each publishes an `aither-manifest.json` beside its page, so any surface can read every sibling's — the network is browsable from any node in it.

| repo | what it is | pages |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | Build AI agent fleets — 3 lines, any backend, local or cloud | [docs](https://aitherium.github.io/awdk/) |
| [awskills](https://github.com/Aitherium/awskills) | Portable agent skills — self-contained procedures an agent loads on demand | [docs](https://aitherium.github.io/awskills/) |
| [awm](https://github.com/Aitherium/awm) | A portable, scoped agent memory | [docs](https://aitherium.github.io/awm/) |
| [awnode](https://github.com/Aitherium/awnode) | A lightweight local gateway — bridges your apps to the AI backends you chose | [docs](https://aitherium.github.io/awnode/) |
| [awrun](https://github.com/Aitherium/awrun) | A priority-aware queue and dispatcher for agentic runs and ad-hoc CI builds | [docs](https://aitherium.github.io/awrun/) |
| [awgraph](https://github.com/Aitherium/awgraph) | A semantic code graph for agents — AST + tree-sitter, call graphs | [docs](https://aitherium.github.io/awgraph/) |
| **awgit** _(you are here)_ | Semantic version control on top of git — edit-ops and leases | [docs](https://aitherium.github.io/awgit/) |
| [awseal](https://github.com/Aitherium/awseal) | Sign an artifact so a stranger can verify it | [docs](https://aitherium.github.io/awseal/) |
| [awshare](https://github.com/Aitherium/awshare) | Publish an artifact and fetch it back verified | [docs](https://aitherium.github.io/awshare/) |
| [awnest](https://github.com/Aitherium/awnest) | Prove there is a human before you let them into the nest | [docs](https://aitherium.github.io/awnest/) |
| [awnboard](https://github.com/Aitherium/awnboard) | A front gate you can put in front of anything, and hand someone the key to | [docs](https://aitherium.github.io/awnboard/) |
| [awnix](https://github.com/Aitherium/awnix) | A Linux you can hand to an agent — immutable base, capabilities included | [docs](https://aitherium.github.io/awnix/) |
| [awrecover](https://github.com/Aitherium/awrecover) | Labelled snapshots with an all-or-nothing restore | [docs](https://aitherium.github.io/awrecover/) |
| [awrelay](https://github.com/Aitherium/awrelay) | Portable agent messaging — findings, alerts, coordination | [docs](https://aitherium.github.io/awrelay/) |
| [awmail](https://github.com/Aitherium/awmail) | Give an agent an email address — send, and actually receive | [docs](https://aitherium.github.io/awmail/) |
| [awfind](https://github.com/Aitherium/awfind) | A portable search client — query, results, ranking | [docs](https://aitherium.github.io/awfind/) |
| [awbrowse](https://github.com/Aitherium/awbrowse) | A portable browser client — navigate, console, network, DOM, screenshot | [docs](https://aitherium.github.io/awbrowse/) |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | Near-optimal KV cache quantization for LLM inference — sub-byte compression | [docs](https://aitherium.github.io/aitherkvcache/) |
| [AitherZero](https://github.com/Aitherium/AitherZero) | PowerShell 7+ automation framework — numbered, self-describing scripts | [docs](https://aitherium.github.io/AitherZero/) |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | Browser extension — federated AI search, page context, and the Living OS overlay | [docs](https://aitherium.github.io/AitherConnect/) |

<!-- aither-ecosystem:end -->
