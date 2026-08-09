# awgit — Aither World-Graph git

Git was built for human commit speed. It has no world model — it doesn't know
what a function is, only which lines moved. At agent scale — a fleet of agents
committing to one shared tree every few minutes — that becomes the bottleneck.

**awgit rides on top of git** (git stays the byte-truth, the transport, the
history) and adds the layer git was missing: a **world model as the database**,
**semantic edit-ops as transactions**, **verified identity**, a **ledger**, and
**differential sync** that teleports deltas instead of copying trees.

## The idea: the world model IS the database

Every commit becomes an `EditOp` — which functions/classes/methods changed, the
old and new bodies by git-blob content address, the actor, the parent chain.
Ops key on **stable node ids** — a content-independent UUID per `(name, path)`
that survives renames and moves. The op-log is a **differential index**: only
what changed, never the snapshot.

- **Merge** — at node granularity, not line granularity. Disjoint node sets
  merge clean by construction; shared nodes that genuinely collide escalate to
  a human naming the exact function.
- **Leases** — heartbeat-renewed TTL. A vanished agent's leases free their
  targets on their own; two agents with leases never collide.
- **Verified identity** — every op records the actor plus the box's VERIFIED
  GitHub login (via `gh`), resolved automatically and cached. The op-log
  doesn't take anyone's word for who did what.
- **Attribution** — every op records who changed what under a verified GitHub
  identity, with a deterministic `ledger_ref`; `awgit ledger` is the read-only
  attribution view.
- **Content-addressed bodies** — op bodies materialize into a deduped store, so
  any op reconstructs any node body **without the original git blobs**. Content
  addressing *is* dedupe: identical bodies across commits, branches and
  worktrees collapse to one blob, and `awgit dedupe` quantifies + reclaims the
  disk duplication.
- **Differential sync** — `awgit sync export` emits the ops a peer is missing
  (parent-first) plus the bodies they reference; `awgit sync import` applies
  idempotently, so a bundle applied twice converges. A caught-up peer gets a
  tiny delta; a fresh endpoint gets the full clone.

## Install

```bash
pip install awgit
# or from source:
git clone https://github.com/aitherium/awgit && cd awgit
pip install -e .
```

Needs Python 3.10+ and `git`. The post-commit hook needs `gh` on PATH to resolve
a verified GitHub identity (best-effort — capture never depends on it).

## Set up

```bash
awgit init        # verify the actor: gh auth, resolve the verified GitHub login
awgit hooks install     # chained post-commit capture — preserves your existing hooks
awgit hooks uninstall   # remove the chain, restore the original hooks
```

Every commit from now on is captured as a semantic edit-op. See:

```bash
awgit status              # op-log status: how many ops, bodies, coverage
awgit diff <sha> <sha>    # node-level diff between two commits
awgit ledger --sha <sha>                 # attribution for one commit
awgit sync export --known <ids> -o delta.json   # teleport deltas to a peer
awgit sync import delta.json                    # idempotent convergence
awgit dedupe --scan <trees...>                  # quantify disk duplication
awgit dedupe --reclaim <trees...> --apply       # hard-link identical files
```

The durable store lives **outside** the git tree at
`~/.aither/awgit/data` (override with `VCS_DATA_ROOT`), so the op-log and body
store never pollute your repository or your clones.

## CLI

```bash
awgit capture --sha <sha>     # capture one commit as an EditOp (hooks do this)
awgit merge-preview <a> <b>   # node-level merge preview of two shas
awgit merge-conflicts         # list escalated conflicts awaiting a human
awgit lease acquire <targets...> / heartbeat / release / list / sweep
```

## Using with Claude Code / an agent

Install the hooks, then commit as normal — capture is automatic. After a commit:

```bash
awgit status      # what changed, and who (verified GitHub identity)
awgit ledger --sha <sha>      # attribution: who changed what on this commit
awgit sync export -o delta.json   # hand the delta to a peer node
```

See the `awgit-claude-code` skill for the full workflow.

## Tests

```bash
cd awgit && python -m pytest tests/ -q
```

## License

Apache-2.0. Built by Aitherium — the git that scales to agents.
