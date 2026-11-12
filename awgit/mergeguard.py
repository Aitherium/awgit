"""Refuse to merge a pull request nobody verified and nobody reviewed.

Measured 2026-11-12: 126 pull requests merged in one day; of 20 sampled, all 20
had cancelled checks and 0 reviews. The hosted runners could not answer (about
206 runs queued, the oldest about 25 hours), so sessions merged on a local test
run that nothing recorded. A merge that leaves no evidence is indistinguishable
from a merge that skipped the checks.

The guard asks GitHub two questions about the pull request's HEAD commit:

1. **Was it verified?** Either the commit status ``local-verify`` is ``success``
   (posted by whatever verifies a pull request on your own machines), or the
   hosted check runs passed: every run completed, none failed, none was
   cancelled, at least one succeeded. A ``local-verify`` that is ``failure`` or
   ``error`` refuses the merge whatever the hosted checks say.
2. **Was it reviewed?** Either a GitHub review exists for that same commit, or a
   local review record exists for it: a JSON file at ``review_record_path()``
   with ``head_sha`` and ``verdict``. A review that asks for changes refuses the
   merge. A record that says the reviewer could not judge does not count.

A repository tells its contributors HOW to get both by listing the commands in
``.github/merge-guard-hints.txt`` (one per line, ``{pr}`` is replaced); the
guard prints them with every refusal.

Anything the guard cannot read is a refusal, never a pass.

The owner can override with a reason. An override needs an interactive
terminal (an agent's shell has none), is appended to
``~/.aither/merge-guard/overrides.jsonl`` and is posted on the commit as the
status ``merge-guard`` so it is visible on the pull request.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

VERIFY_CONTEXT = "local-verify"
OVERRIDE_CONTEXT = "merge-guard"

#: Check-run conclusions that mean "this did not pass".
BAD_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out", "action_required",
                             "startup_failure", "stale"})
#: Review verdicts in a local record that are NOT a review.
UNJUDGED_VERDICTS = frozenset({"", "could-not-judge", "dead", "error"})

EXIT_OK, EXIT_REFUSED, EXIT_DEAD = 0, 1, 2

Runner = Callable[[list], "tuple[int, str, str]"]


def _run(argv: list) -> "tuple[int, str, str]":
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after 120s"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def state_dir() -> Path:
    return Path(os.environ.get("AITHER_HOME") or (Path.home() / ".aither"))


def review_record_path(repo: str, number: int, head_sha: str) -> Path:
    """Where pr_local_review.py records a review of one head commit."""
    return (state_dir() / "local-review" / repo.replace("/", "__")
            / f"pr{number}-{head_sha}.json")


@dataclass
class Facts:
    """What GitHub (and this machine) say about one pull request head."""

    repo: str
    number: int
    head_sha: str = ""
    state: str = ""
    statuses: list = field(default_factory=list)      # [{context, state}]
    check_runs: list = field(default_factory=list)    # [{name, status, conclusion}]
    reviews: list = field(default_factory=list)       # [{state, commit_id, user}]
    local_review: Optional[dict] = None
    errors: list = field(default_factory=list)        # what could not be read


@dataclass
class Decision:
    allowed: bool
    dead: bool
    verified_by: str
    reviewed_by: str
    reasons: list

    @property
    def exit_code(self) -> int:
        if self.allowed:
            return EXIT_OK
        return EXIT_DEAD if self.dead else EXIT_REFUSED


def _json_lines(text: str) -> list:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def fetch(repo: str, number: int, run: Runner = _run) -> Facts:
    """Read the head commit, its statuses, its check runs and the PR's reviews."""
    facts = Facts(repo=repo, number=number)
    code, out, err = run(["gh", "api", f"repos/{repo}/pulls/{number}",
                          "--jq", "{sha: .head.sha, state: .state}"])
    if code != 0:
        facts.errors.append(f"pull request unreadable: {err.strip() or code}")
        return facts
    try:
        head = json.loads(out)
        facts.head_sha, facts.state = str(head["sha"]), str(head["state"])
    except (ValueError, KeyError, TypeError) as exc:
        facts.errors.append(f"pull request answer unparseable: {exc}")
        return facts
    sha = facts.head_sha
    for label, argv, target in (
        ("commit statuses",
         ["gh", "api", f"repos/{repo}/commits/{sha}/status", "--paginate",
          "--jq", ".statuses[] | {context, state}"], "statuses"),
        ("check runs",
         ["gh", "api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100",
          "--paginate", "--jq", ".check_runs[] | {name, status, conclusion}"],
         "check_runs"),
        ("reviews",
         ["gh", "api", f"repos/{repo}/pulls/{number}/reviews?per_page=100",
          "--paginate",
          "--jq", ".[] | {state, commit_id, user: .user.login}"], "reviews"),
    ):
        code, out, err = run(argv)
        if code != 0:
            facts.errors.append(f"{label} unreadable: {err.strip() or code}")
            continue
        try:
            setattr(facts, target, _json_lines(out))
        except ValueError as exc:
            facts.errors.append(f"{label} unparseable: {exc}")
    record = review_record_path(repo, number, sha)
    if record.is_file():
        try:
            facts.local_review = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            facts.errors.append(f"local review record unreadable: {exc}")
    return facts


def _verification(facts: Facts) -> "tuple[str, list]":
    """('' or what verified it, reasons it is not verified)."""
    local = [s for s in facts.statuses if s.get("context") == VERIFY_CONTEXT]
    if local:
        state = str(local[0].get("state") or "")
        if state == "success":
            return VERIFY_CONTEXT, []
        return "", [f"{VERIFY_CONTEXT} on {facts.head_sha[:10]} is {state or 'unknown'}, "
                    "not success"]
    runs = facts.check_runs
    if not runs:
        return "", [f"no {VERIFY_CONTEXT} status and no hosted check run on "
                    f"{facts.head_sha[:10]}"]
    unfinished = [r for r in runs if r.get("status") != "completed"]
    bad = [r for r in runs if r.get("conclusion") in BAD_CONCLUSIONS]
    passed = [r for r in runs if r.get("conclusion") == "success"]
    if bad or unfinished or not passed:
        parts = []
        if bad:
            names = ", ".join(sorted({f"{r.get('name')}={r.get('conclusion')}"
                                      for r in bad})[:4])
            parts.append(f"{len(bad)} hosted check(s) did not pass ({names})")
        if unfinished:
            parts.append(f"{len(unfinished)} hosted check(s) never finished")
        if not passed:
            parts.append("no hosted check succeeded")
        return "", [f"no {VERIFY_CONTEXT} status; " + "; ".join(parts)]
    return f"hosted checks ({len(passed)} passed)", []


def _review(facts: Facts) -> "tuple[str, list]":
    on_head = [r for r in facts.reviews if r.get("commit_id") == facts.head_sha]
    latest: dict = {}
    for review in on_head:                       # the API lists oldest first
        latest[str(review.get("user") or "")] = str(review.get("state") or "")
    blocking = sorted(u for u, s in latest.items() if s == "CHANGES_REQUESTED")
    if blocking:
        return "", ["changes requested on this commit by " + ", ".join(blocking)]
    counted = sorted(u for u, s in latest.items() if s in ("APPROVED", "COMMENTED"))
    if counted:
        return "GitHub review by " + ", ".join(counted), []
    record = facts.local_review
    if record is not None:
        verdict = str(record.get("verdict") or "").lower()
        if record.get("head_sha") != facts.head_sha:
            return "", ["the local review record is for another commit"]
        if verdict in UNJUDGED_VERDICTS:
            return "", [f"the local review could not judge ({verdict or 'no verdict'})"]
        if verdict == "changes-requested":
            return "", ["the local review found blocking problems"]
        return f"local review ({verdict}, {record.get('findings_count', '?')} finding(s))", []
    stale = len(facts.reviews) - len(on_head)
    hint = f" ({stale} review(s) are for an older commit)" if stale else ""
    return "", [f"no review recorded for {facts.head_sha[:10]}{hint}"]


def evaluate(facts: Facts) -> Decision:
    """Pure: the same facts always give the same decision."""
    if facts.errors:
        return Decision(False, True, "", "",
                        ["could not judge: " + e for e in facts.errors])
    if not facts.head_sha:
        return Decision(False, True, "", "", ["could not judge: no head commit"])
    verified_by, why_v = _verification(facts)
    reviewed_by, why_r = _review(facts)
    reasons = why_v + why_r
    return Decision(not reasons, False, verified_by, reviewed_by, reasons)


HINTS_FILE = ".github/merge-guard-hints.txt"


def load_hints(run: Runner = _run) -> list:
    """The repository's own "how to get verified" lines, or [] when it has none."""
    code, out, _ = run(["git", "rev-parse", "--show-toplevel"])
    if code != 0 or not out.strip():
        return []
    path = Path(out.strip()) / HINTS_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")][:8]


def explain(facts: Facts, decision: Decision, hints: Optional[list] = None) -> str:
    """The refusal (or pass) as the lines an agent should read."""
    head = facts.head_sha[:10] or "?"
    if decision.allowed:
        return (f"merge guard: PR #{facts.number} @ {head} verified by "
                f"{decision.verified_by}; reviewed by {decision.reviewed_by}")
    lines = [f"merge guard: REFUSING to merge PR #{facts.number} @ {head}"]
    lines += [f"  - {reason}" for reason in decision.reasons]
    lines += ["  " + hint.replace("{pr}", str(facts.number)) for hint in (hints or [])]
    lines += [
        f"  Needed on the head commit: a `{VERIFY_CONTEXT}` status that is success "
        "(or passing hosted checks) AND a recorded review.",
        "  A local test run nobody recorded is not verification. Only the owner "
        "may pass --owner-override \"<reason>\" (interactive terminal).",
    ]
    return "\n".join(lines)


def record_override(facts: Facts, decision: Decision, reason: str, *,
                    run: Runner = _run, is_tty: Optional[bool] = None,
                    confirm: Optional[Callable[[str], str]] = None,
                    who: str = "") -> "tuple[bool, str]":
    """Log an owner override. (ok, message). Never silent, never from an agent shell."""
    reason = " ".join(str(reason or "").split())
    if len(reason) < 10:
        return False, "an override needs a real reason (10+ characters)"
    if not facts.head_sha:
        return False, "cannot override: the head commit is unknown"
    # Both streams: on Windows the NUL device reports isatty() True, so a shell
    # run with `< NUL` would pass a stdin-only test (measured 2026-11-12).
    tty = (sys.stdin.isatty() and sys.stdout.isatty()) if is_tty is None else is_tty
    if not tty:
        return False, ("an override needs an interactive terminal: it is the "
                       "owner's decision, and an agent session has none")
    ask = confirm or input
    try:
        typed = ask(f"Override the merge guard for PR #{facts.number}? "
                    f"Type the PR number to confirm: ")
    except (EOFError, KeyboardInterrupt):
        typed = ""
    if str(typed).strip() != str(facts.number):
        return False, "override not confirmed"
    if not who:
        code, out, _ = run(["gh", "api", "user", "--jq", ".login"])
        who = out.strip() if code == 0 and out.strip() else "unknown"
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "repo": facts.repo, "pr": facts.number, "head_sha": facts.head_sha,
             "by": who, "reason": reason, "refused_for": decision.reasons}
    log = state_dir() / "merge-guard" / "overrides.jsonl"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError as exc:
        return False, f"override refused: could not write the log ({exc})"
    description = f"OVERRIDDEN by {who}: {reason}"[:140]
    code, _, err = run(["gh", "api", "-X", "POST",
                        f"repos/{facts.repo}/statuses/{facts.head_sha}",
                        "-f", "state=error", "-f", f"context={OVERRIDE_CONTEXT}",
                        "-f", f"description={description}"])
    posted = "posted on the commit" if code == 0 else \
        f"NOT posted on the commit ({err.strip()[:120] or code})"
    return True, f"override logged to {log} and {posted}"


def split_override(rest: list) -> "tuple[list, Optional[str]]":
    """Pull ``--owner-override <reason>`` out of forwarded gh arguments."""
    out, reason, i = [], None, 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--owner-override":
            reason = rest[i + 1] if i + 1 < len(rest) else ""
            i += 2
            continue
        if arg.startswith("--owner-override="):
            reason = arg.split("=", 1)[1]
            i += 1
            continue
        out.append(arg)
        i += 1
    return out, reason


def pr_number_from(rest: list, run: Runner = _run) -> Optional[int]:
    """The PR a forwarded ``gh pr merge`` names, or the current branch's."""
    takes_value = {"-R", "--repo", "-b", "--body", "-F", "--body-file", "-t",
                   "--subject", "--match-head-commit", "-A", "--author-email"}
    skip = False
    for arg in rest:
        if skip:
            skip = False
            continue
        if arg in takes_value:
            skip = True
            continue
        if arg.isdigit():
            return int(arg)
        if "/pull/" in arg:
            tail = arg.rstrip("/").rsplit("/pull/", 1)[1].split("/")[0]
            if tail.isdigit():
                return int(tail)
    code, out, _ = run(["gh", "pr", "view", "--json", "number", "--jq", ".number"])
    return int(out.strip()) if code == 0 and out.strip().isdigit() else None


def repo_from(rest: list, run: Runner = _run) -> Optional[str]:
    for i, arg in enumerate(rest):
        if arg in ("-R", "--repo") and i + 1 < len(rest):
            return rest[i + 1]
        if arg.startswith("--repo="):
            return arg.split("=", 1)[1]
    code, out, _ = run(["gh", "repo", "view", "--json", "nameWithOwner",
                        "--jq", ".nameWithOwner"])
    return out.strip() if code == 0 and out.strip() else None


def guard(repo: str, number: int, override_reason: Optional[str] = None, *,
          run: Runner = _run, is_tty: Optional[bool] = None,
          confirm: Optional[Callable[[str], str]] = None,
          out=None) -> int:
    """0 = merge may proceed. 1 = refused. 2 = could not judge (also a refusal)."""
    out = out or sys.stderr
    facts = fetch(repo, number, run)
    decision = evaluate(facts)
    print(explain(facts, decision, None if decision.allowed else load_hints(run)),
          file=out)
    if decision.allowed:
        return EXIT_OK
    if override_reason is None:
        return decision.exit_code
    ok, message = record_override(facts, decision, override_reason, run=run,
                                  is_tty=is_tty, confirm=confirm)
    print(f"merge guard: {message}", file=out)
    return EXIT_OK if ok else decision.exit_code


def main(argv: Optional[list] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="awgit-merge-guard",
        description="May this pull request be merged? Exit 0 yes, 1 refused, "
                    "2 could not judge.")
    ap.add_argument("number", type=int)
    ap.add_argument("--repo", default="")
    ap.add_argument("--owner-override", dest="override", default=None, metavar="REASON")
    args = ap.parse_args(argv)
    repo = args.repo or repo_from([]) or ""
    if not repo:
        print("merge guard: could not tell which repository this is; pass --repo",
              file=sys.stderr)
        return EXIT_DEAD
    return guard(repo, args.number, args.override)


if __name__ == "__main__":
    sys.exit(main())
