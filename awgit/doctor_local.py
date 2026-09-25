"""awgit's own lines for ``awgit doctor`` (the hook ``_doctor.py`` imports).

The one fact the stack picture cannot show: WHICH store this awgit reads and
writes. An embedding can resolve a different store than the standalone package
(the AitherOS overlay answers ``Paths.DATA/vcs``), and two leases in two stores
cannot see each other -- so the doctor names the resolved path, never a default.
"""

from __future__ import annotations

from typing import List


def _doctor_local() -> List[str]:
    from .data_root import vcs_data_root

    return [f"store      {vcs_data_root()}  ($VCS_DATA_ROOT overrides)"]
