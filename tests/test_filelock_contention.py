"""FileLock waits out a lock held by another process instead of failing at 10 s.

``msvcrt.LK_LOCK`` gives up after ten one-second retries with EDEADLOCK
("Resource deadlock avoided"); under many concurrent sessions the lease
registry's lock is held longer than that and every pre-commit lease check died.
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest
from awgit import oplog

_HOLDER = r"""
import sys, time
from pathlib import Path
from awgit.oplog import FileLock
with FileLock(Path(sys.argv[1])):
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt byte locks are Windows-only")
def test_a_lock_held_longer_than_lk_lock_gives_up_is_still_acquired(tmp_path):
    path = tmp_path / "leases.lock"
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(path), "12"],
                              stdout=subprocess.PIPE, text=True, encoding="utf-8",
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        assert holder.stdout.readline().strip() == "held"
        t0 = time.monotonic()
        with oplog.FileLock(path):
            waited = time.monotonic() - t0
        assert waited >= 10.0          # it really waited past LK_LOCK's 10 s limit
    finally:
        holder.wait(30)


@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt byte locks are Windows-only")
def test_a_lock_that_never_frees_still_fails_after_the_wait(tmp_path, monkeypatch):
    path = tmp_path / "leases.lock"
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(path), "8"],
                              stdout=subprocess.PIPE, text=True, encoding="utf-8",
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        assert holder.stdout.readline().strip() == "held"
        monkeypatch.setattr(oplog, "LOCK_WAIT_S", 1.0)
        import msvcrt
        with open(path, "a+b") as fh, pytest.raises(OSError):
            oplog._msvcrt_lock(msvcrt, fh, wait_s=1.0)
    finally:
        holder.wait(30)
