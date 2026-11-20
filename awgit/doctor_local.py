"""awgit's own lines for ``awgit doctor`` (the hook ``_doctor.py`` imports).

The one fact the stack picture cannot show: WHICH store this awgit reads and
writes. An embedding can resolve a different store than the standalone package
(the AitherOS overlay answers ``Paths.DATA/vcs``), and two leases in two stores
cannot see each other -- so the doctor names the resolved path, never a default.

It also reports repo integrity (``awgit integrity``): a managed repo whose
``.git/config`` is all NUL bytes makes every git command fatal, and a doctor that
printed "awgit ok" over that would be wrong in the one way that matters.
"""

from __future__ import annotations

from typing import List, Tuple


def _integrity_rows() -> list:
    from .config_integrity import classify, discover, snapshots

    # Read-only here: a doctor never writes snapshots or restores.
    rows = []
    for common in discover():
        cfg = common / "config"
        state, why = classify(cfg)
        rows.append({"config": str(cfg), "state": state, "why": why,
                     "snapshots": len(snapshots(common))})
    return rows


def _doctor_local() -> List[str]:
    from .data_root import vcs_data_root

    lines = [f"store      {vcs_data_root()}  ($VCS_DATA_ROOT overrides)"]
    try:
        for r in _integrity_rows():
            lines.append(f"config     {r['state']:<11} {r['config']}  "
                         f"({r['snapshots']} snapshot(s)){'  ' + r['why'] if r['why'] else ''}")
    except Exception as exc:  # noqa: BLE001 - the doctor reports, never raises
        lines.append(f"config     could not check: {type(exc).__name__}: {exc}")
    return lines


def _doctor_local_verdict() -> Tuple[List[str], List[str]]:
    problems: List[str] = []
    unjudged: List[str] = []
    try:
        for r in _integrity_rows():
            if r["state"] != "ok":
                problems.append(f"{r['config']} is {r['state']} ({r['why']}) -- "
                                f"`awgit integrity --fix` restores the last good copy")
            elif r["snapshots"] == 0:
                unjudged.append(f"{r['config']} has no snapshot yet -- a crash now would "
                                f"leave nothing to restore; run `awgit integrity`")
    except Exception as exc:  # noqa: BLE001
        unjudged.append(f"repo integrity: {type(exc).__name__}: {exc}")
    return problems, unjudged
