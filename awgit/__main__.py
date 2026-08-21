"""`python -m awgit` — the invocation that still works when the console shim does not.

WHY THIS EXISTS

Measured 2026-08-21: `awgit.exe` on this host exits 126 from bash and, asked directly,
PowerShell says why -- "An Application Control policy has blocked this file". WDAC blocks
the freshly generated shim, so `pip install -e` cannot repair it: reinstalling regenerates
the same blocked binary.

That matters more than a broken convenience. The pre-commit lease gate is invoked as
`awgit lease acquire <file>`, so with the shim blocked EVERY commit in this repo either
fails the gate or has to be talked around one file at a time -- while `pip show`, `import
awgit` and `shutil.which('awgit')` all report the tool perfectly healthy. That is the
"looks installed, fails on first real use" shape the tooling checker was written for, one
level below where it was looking; ATI006 now asserts the shim actually RUNS.

A module entry point needs no shim, no new executable and no security-policy exception:
`python -m awgit ...` goes through the interpreter, which is already trusted. Anything
that shells the console script keeps working unchanged where the policy allows it.

Kept to a delegation on purpose -- the CLI lives in `cli.py` and having two entry points
that can disagree about argument handling is its own defect.
"""
from __future__ import annotations

import sys

from awgit.cli import main

if __name__ == "__main__":
    sys.exit(main())
