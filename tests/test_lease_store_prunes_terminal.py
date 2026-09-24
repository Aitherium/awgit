"""The lease store must not grow without bound (D-2211).

Expired and released leases were kept forever: measured 2026-08-25 the store held
9,661 entries with 2 active (4.5 MB), and 2026-09-23 it held 4,937 with 1 active
(2.3 MB) -- re-parsed and re-written under the store lock on every acquire.

Asserted from both directions: old terminal leases are dropped on the next
mutation, while an ACTIVE lease (whatever its age) and a RECENT terminal lease
survive. Mutation guard: removing the ``_prune_terminal()`` calls from
``acquire``/``sweep_expired`` fails ``test_old_terminal_leases_are_pruned``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from awgit.leases import LeaseRegistry


def _ts(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _row(lid: str, status: str, days_ago: float, *, ttl_days: float = 0.0) -> dict:
    return {
        "lease_id": lid,
        "actor": "peer",
        "kind": "path",
        "target": f"f/{lid}.py",
        "granted_ts": _ts(days_ago),
        "expires_ts": _ts(days_ago - ttl_days),
        "heartbeat_ts": _ts(days_ago),
        "ttl_sec": 300,
        "reason": "",
        "status": status,
    }


def _seed(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    rows = [
        _row("old-expired", "expired", 30),
        _row("old-released", "released", 30),
        _row("old-revoked", "revoked", 30),
        _row("fresh-released", "released", 0.01),
        _row("fresh-expired", "expired", 1),
        # An ACTIVE lease granted long ago with a far-future expiry: never pruned.
        _row("old-active", "active", 30, ttl_days=60),
    ]
    (root / "leases.json").write_text(json.dumps({"leases": rows}), encoding="utf-8")


def _stored_ids(root: Path) -> set:
    data = json.loads((root / "leases.json").read_text(encoding="utf-8"))
    return {d["lease_id"] for d in data["leases"]}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("AWGIT_LEASE_RETAIN_DAYS", raising=False)
    root = tmp_path / "vcs"
    _seed(root)
    return root


def test_old_terminal_leases_are_pruned(store):
    reg = LeaseRegistry(data_root=store)
    reg.sweep_expired()
    assert _stored_ids(store) == {"fresh-released", "fresh-expired", "old-active"}


def test_acquire_prunes_too(store, monkeypatch):
    monkeypatch.setattr("awgit.leases.snapshot_baseline", lambda *a, **k: "")
    reg = LeaseRegistry(data_root=store)
    granted = reg.acquire("me", ["new/target.py"], ttl_sec=60)
    ids = _stored_ids(store)
    assert "old-expired" not in ids and "old-released" not in ids
    assert {"fresh-released", "fresh-expired", "old-active", granted[0].lease_id} <= ids
    assert [lz.lease_id for lz in reg.active_leases()].count("old-active") == 1


def test_retention_zero_disables_pruning(store, monkeypatch):
    monkeypatch.setenv("AWGIT_LEASE_RETAIN_DAYS", "0")
    LeaseRegistry(data_root=store).sweep_expired()
    assert len(_stored_ids(store)) == 6


def test_bad_retention_value_falls_back_to_default(store, monkeypatch):
    monkeypatch.setenv("AWGIT_LEASE_RETAIN_DAYS", "forever")
    LeaseRegistry(data_root=store).sweep_expired()
    assert _stored_ids(store) == {"fresh-released", "fresh-expired", "old-active"}
