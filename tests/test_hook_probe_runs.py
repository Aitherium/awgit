"""The probe every hook fragment runs must actually SUCCEED against this CLI.

Each fragment decides whether the `awgit` console script is usable with
`awgit <probe args> >/dev/null 2>&1`. The probe was `--version`, which the CLI
did not define, so argparse exited 2 on every commit: the console script was
never chosen, and every fragment paid a wasted interpreter launch and then fell
through to `python3` (on Windows, the Microsoft Store alias stub) before a real
interpreter. Measured ~8 s per hooked commit; the awgit suite timed out on it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))

from awgit import __version__  # noqa: E402
from awgit.cli import main  # noqa: E402

HOOKS = PKG_ROOT / "awgit" / "hooks"
_PROBE = re.compile(r"if awgit ([^>]+?)\s*>/dev/null 2>&1; then")


def _probes() -> list:
    found = []
    for frag in sorted(HOOKS.glob("*.d/*")):
        m = _PROBE.search(frag.read_text(encoding="utf-8"))
        if m:
            found.append((frag.relative_to(HOOKS).as_posix(), m.group(1).split()))
    return found


def test_the_fragments_carry_a_probe_at_all():
    # If the probe line changes shape this test must fail loudly, not pass on
    # an empty parametrisation.
    assert len(_probes()) >= 3, _probes()


@pytest.mark.parametrize("frag,args", _probes())
def test_each_fragment_probe_exits_zero(frag, args, capsys):
    try:
        rc = main(list(args))
    except SystemExit as exc:  # argparse's version action exits
        rc = exc.code
    assert rc in (0, None), f"{frag}: `awgit {' '.join(args)}` exited {rc}"


def test_version_flag_prints_the_version_in_a_subprocess():
    proc = subprocess.run(
        [sys.executable, "-m", "awgit.cli", "--version"], cwd=str(PKG_ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"awgit {__version__}"
