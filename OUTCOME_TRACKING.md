# Outcome Tracking — Pairing Code Changes with Verification Results

## Overview

awgit now durably records the outcomes of gate verification, creating a training signal that pairs:
- **EditOp**: what code changed, who changed it, when  
- **Outcome**: which gates verified that commit and what they said

This transforms `awgit prove` from a one-shot display into a durable record, building a dataset where a machine can learn: which gates matter, whether they prevent real failures, and which ones cry wolf.

## The Key Distinction: "Unknown" vs "Passed"

An Outcome can have three verdicts:

| verdict | meaning | signal |
|---------|---------|--------|
| **unknown** | no gates ran / nobody checked | absence of evidence (no training value) |
| **passed** | gates ran, all returned "ok", nothing dead | positive verification |
| **failed** | one or more violations or dead gates | negative verification |

**This distinction is load-bearing.** Conflating unknown with passed would poison a training signal: a commit nobody checked would appear identical to one that passed all checks, which is worse than no signal at all.

The difference is enforced by tests with mutation guards (see `test_outcome_tracking.py`).

## Data Structure

### Outcome (in schema.py)

```python
@dataclass
class Outcome:
    outcome_id: str          # unique id (sha[:16] + timestamp)
    git_sha: str             # which commit was verified
    ts: str                  # when the verification ran (ISO8601)
    gates: List[OutcomeGate] # results from each gate
    verdict: str             # "unknown", "passed", or "failed" (computed)
```

### OutcomeGate

```python
@dataclass
class OutcomeGate:
    name: str                # gate name
    status: str              # "ok", "violation", or "dead"
    detail: str              # human-readable result (truncated to 400 chars)
```

## Durability

Outcomes are appended to `~/.aither/awgit/data/outcomes.jsonl` (alongside the oplog), JSONL with:
- OS-level exclusive lock (Windows msvcrt / POSIX fcntl)
- fsync on every append
- Idempotent by outcome_id

## Usage

### Automatic Recording (Recommended)

Add `--prove` to the post-commit hook's `awgit capture` invocation:

```bash
awgit capture --sha "$1" --actor "$ACTOR" --prove
```

This runs gates immediately after capture and records the outcome.

### Manual Recording

Outcomes can also be recorded explicitly after a commit:

```python
from awgit.outcomes import record_outcome
from awgit.prove import run_gates

gates = run_gates(["path/to/file.py"])
outcome = record_outcome("abc123def", gates)
print(f"Recorded: {outcome.verdict}")
```

### Querying Outcomes

```python
from awgit.outcomes import OutcomeLog

log = OutcomeLog()

# All outcomes for a commit
outcomes = log.outcomes_for_commit("abc123def")

# The most recent outcome (useful for "has this commit been checked?")
latest = log.latest_outcome("abc123def")
if latest and latest.verdict == "passed":
    print("This commit passed verification")
elif latest is None:
    print("This commit has never been checked")
```

## Integration with Training

The outcome record is the bridge between verification and learning:

1. **Collection**: post-commit hook records outcomes automatically
2. **Aggregation**: outcome_log accumulates records over commits  
3. **Training**: an external system reads the outcome log and builds:
   - Which gates correlate with real failures?
   - Which gates are noisiest?
   - Do violations actually prevent bugs?

## Why This Matters

Without outcome recording, `awgit prove` is a tool that computes a verdict once and discards it. With recording:

- **Replayable verification**: ask "what were the verification results for this commit?" and get the answer from the log, not from re-running (which might give different results now)
- **Signal pairing**: machine learning systems can correlate gate verdicts with actual outcomes
- **Audit trail**: prove the commit was checked, when, and by what
- **Drift detection**: if a gate that used to pass starts failing on old commits (or vice versa), the record makes that visible

## Limitations (Deliberate Non-Goals)

This implementation does NOT:

- **Detect reverts**: if a commit is reverted, the oplog will show it, but the outcome log records verdicts independently. Linking outcomes to their reversions is a separate, harder problem.
- **Distinguish "unknown" into subcategories**: there are many reasons no gates ran (none registered, all skipped, runner crashed), but they all read as absence of evidence and should not be conflated with a successful pass.
- **Gate on outcome verdicts**: outcome recording is orthogonal to enforcement. A gate that fails can still be committed; the outcome record just documents that it failed.
- **Provide timeline queries**: outcomes are a log of records, not a time series. A consumer that needs "was this gate failing between dates X and Y?" must scan and aggregate the log itself.

## Testing

See `test_outcome_tracking.py` for:
- Verdict classification tests (unknown/passed/failed)
- Mutation guards proving the unknown/passed distinction cannot collapse
- Durability and idempotency tests
- Schema versioning and forward compatibility
