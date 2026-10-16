"""Coordination that survives a second machine: leases and op-log sync via a gateway.

Two claims, both against the REAL handlers in ``awgit.mcp``:

* op-log deltas travel as ``vcs_sync_export`` / ``vcs_sync_import`` bundles, and
  an export -> import between two stores converges idempotently (and pages when
  a delta is over the cap, and refuses bodies that do not hash to their key);
* with ``AWGIT_LEASE_URL`` set, ``awgit lease ...`` talks to the gateway's
  ``vcs_lease_*`` tools -- the lease lands in the GATEWAY's store, not the local
  file -- and a configured gateway that cannot be reached FAILS (exit 2) instead
  of quietly granting a local lease nobody else can see.

The "gateway" here is a real HTTP server on 127.0.0.1 speaking the MCP
streamable-HTTP subset the client uses, dispatching to ``awgit.mcp.TOOLS``.
"""

from __future__ import annotations

import base64
import json
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from awgit import capture_ops, cli, mcp, remote, sync_status
from awgit import sync as sync_mod
from awgit.bodies import BodyStore
from awgit.leases import LeaseRegistry, coverage_gap

SRC = "def alpha(x):\n    return x + 1\n\n\ndef beta(y):\n    return y * 2\n"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace", creationflags=NOWIN,
    ).stdout.strip()


@pytest.fixture
def ops_store(tmp_path):
    """A data root holding three captured ops (three commits to one file)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "test")
    data = tmp_path / "src-data"
    (repo / "m.py").write_text(SRC, encoding="utf-8")
    _git(repo, "add", "m.py")
    _git(repo, "commit", "-q", "-m", "base")
    for i in range(3):
        (repo / "m.py").write_text(SRC.replace("x + 1", f"x + {i + 10}"),
                                   encoding="utf-8")
        _git(repo, "add", "m.py")
        _git(repo, "commit", "-q", "-m", f"c{i}")
        sha = _git(repo, "rev-parse", "HEAD")
        assert capture_ops(sha, actor=f"a{i}", repo_path=str(repo), data_root=data)
    return data


# ── sync over the MCP surface ───────────────────────────────────────────────


def test_sync_export_import_roundtrip_is_idempotent(ops_store, tmp_path):
    dst = tmp_path / "dst-data"
    page = mcp.vcs_sync_export(known_op_ids=[], data_root=str(ops_store))
    assert page["complete"] and page["op_count"] == 3 and page["body_count"] > 0
    assert len(page["bundle"]) == page["bytes"] <= mcp.SYNC_MAX_BUNDLE_BYTES

    first = mcp.vcs_sync_import(page["bundle"], data_root=str(dst))
    second = mcp.vcs_sync_import(page["bundle"], data_root=str(dst))
    assert first["imported_ops"] == 3 and first["rejected_bodies"] == 0
    assert second["imported_ops"] == 0, "re-importing the same bundle duplicated ops"
    assert sync_status(dst)["ops"] == sync_status(ops_store)["ops"] == 3

    # A caught-up peer is owed nothing.
    known = json.loads(base64.b64decode(page["bundle"]))["ops"]
    again = mcp.vcs_sync_export(known_op_ids=[o["op_id"] for o in known],
                                data_root=str(ops_store))
    assert again["op_count"] == 0 and again["complete"]


def test_sync_export_pages_a_delta_over_the_cap(ops_store, tmp_path, monkeypatch):
    full = mcp.vcs_sync_export(data_root=str(ops_store))
    monkeypatch.setattr(mcp, "SYNC_MAX_BUNDLE_BYTES", full["bytes"] - 1)
    page = mcp.vcs_sync_export(data_root=str(ops_store))
    assert not page["complete"] and 0 < page["op_count"] < 3
    assert page["remaining"] == 3 - page["op_count"]
    assert page["bytes"] <= full["bytes"] - 1


def test_sync_import_refuses_a_body_that_does_not_hash_to_its_key(ops_store, tmp_path):
    bundle = json.loads(base64.b64decode(
        mcp.vcs_sync_export(data_root=str(ops_store))["bundle"]))
    sha = next(iter(bundle["bodies"]))
    bundle["bodies"][sha] = base64.b64encode(b"not the body you named").decode()
    res = mcp.vcs_sync_import(json.dumps(bundle), data_root=str(tmp_path / "d"))
    assert res["rejected_bodies"] == 1
    assert not (tmp_path / "d" / "bodies" / sha[:2] / sha).exists()


@pytest.mark.parametrize("bad", ["", "not base64 !!", base64.b64encode(b"[1]").decode(),
                                 json.dumps({"schema": "other", "ops": []})])
def test_sync_import_refuses_garbage(bad, tmp_path):
    res = mcp.vcs_sync_import(bad, data_root=str(tmp_path / "d"))
    assert res.get("error") and res["imported_ops"] == 0


def test_sync_import_refuses_oversized(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "SYNC_MAX_BUNDLE_BYTES", 10)
    res = mcp.vcs_sync_import("A" * 11, data_root=str(tmp_path / "d"))
    assert "cap" in res["error"]


# ── a fake gateway: MCP streamable HTTP -> awgit.mcp.TOOLS ──────────────────


class _Gateway:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.seen_auth = []
        gw = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet test output
                return

            def do_POST(self):  # noqa: N802 -- http.server API
                gw.seen_auth.append(self.headers.get("Authorization"))
                msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "id" not in msg:
                    self.send_response(202)
                    self.end_headers()
                    return
                if msg["method"] == "initialize":
                    result = {"protocolVersion": remote.PROTOCOL, "capabilities": {}}
                else:
                    assert self.headers.get("Mcp-Session-Id") == "s-1", "no session"
                    name = msg["params"]["name"]
                    fn = mcp.TOOLS[name][0]
                    out = fn(**msg["params"]["arguments"], data_root=str(gw.data_root))
                    result = {"content": [{"type": "text", "text": json.dumps(out)}],
                              "isError": False}
                body = json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                   "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "s-1")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    gw = _Gateway(tmp_path / "gateway-data")
    # localhost on purpose: the client must rewrite it to 127.0.0.1.
    monkeypatch.setenv(remote.ENV_URL, f"http://localhost:{gw.port}")
    monkeypatch.setenv(remote.ENV_BEARER, "test-bearer")
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "local-data"))
    yield gw
    gw.close()


def _local_leases(tmp_path):
    return LeaseRegistry(data_root=tmp_path / "local-data").active_leases()


def test_remote_lease_lands_in_the_gateway_store_and_is_mirrored(
        gateway, tmp_path, capsys):
    assert cli.main(["lease", "acquire", "a.py", "--actor", "me", "--ttl", "60"]) == 0
    held = LeaseRegistry(data_root=gateway.data_root).active_leases()
    assert [(lz.target, lz.actor) for lz in held] == [("a.py", "me")]
    # The commit gate and stage-mine read the LOCAL store: the grant is copied
    # there, tagged with the gateway lease it mirrors.
    local = _local_leases(tmp_path)
    assert [(lz.target, lz.actor) for lz in local] == [("a.py", "me")]
    assert f"[gateway lease {held[0].lease_id}]" in local[0].reason
    assert coverage_gap(["a.py"], "me", data_root=tmp_path / "local-data") == []
    assert set(gateway.seen_auth) == {"Bearer test-bearer"}

    capsys.readouterr()
    assert cli.main(["lease", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["target"] for r in rows] == ["a.py"]

    # A peer on another machine is refused by the SAME store.
    assert cli.main(["lease", "acquire", "a.py", "--actor", "peer"]) == 1
    assert "lease conflict" in capsys.readouterr().err

    # Heartbeat and release accept the leased PATH, like the local CLI.
    assert cli.main(["lease", "heartbeat", "a.py", "--actor", "me"]) == 0
    assert cli.main(["lease", "release", "a.py", "--actor", "me"]) == 0
    assert LeaseRegistry(data_root=gateway.data_root).active_leases() == []
    assert _local_leases(tmp_path) == [], "the local mirror outlived the release"
    assert cli.main(["lease", "release", "a.py", "--actor", "me"]) == 1, (
        "releasing nothing must not report success")


def test_remote_release_by_gateway_id_frees_the_mirror(gateway, tmp_path, capsys):
    assert cli.main(["lease", "acquire", "a.py", "--actor", "me"]) == 0
    gid = LeaseRegistry(data_root=gateway.data_root).active_leases()[0].lease_id
    assert cli.main(["lease", "release", gid, "--actor", "me"]) == 0
    assert _local_leases(tmp_path) == []


def test_remote_acquire_held_locally_hands_the_gateway_grant_back(
        gateway, tmp_path, capsys):
    LeaseRegistry(data_root=tmp_path / "local-data").acquire("local-peer", ["b.py"])
    assert cli.main(["lease", "acquire", "b.py", "--actor", "me"]) == 1
    assert "local store" in capsys.readouterr().err
    assert LeaseRegistry(data_root=gateway.data_root).active_leases() == [], (
        "a refused acquire left the gateway lease held")
    assert [lz.actor for lz in _local_leases(tmp_path)] == ["local-peer"]


def test_unreachable_gateway_fails_loud_and_never_falls_back(tmp_path, monkeypatch, capsys):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    monkeypatch.setenv(remote.ENV_URL, f"http://127.0.0.1:{port}")
    monkeypatch.setenv(remote.ENV_BEARER, "x")
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "local-data"))

    with pytest.raises(remote.RemoteLeaseError):
        remote.GatewayClient(f"http://127.0.0.1:{port}", timeout=5).call(
            "vcs_lease_list", {})
    assert cli.main(["lease", "acquire", "a.py", "--actor", "me"]) == 2
    assert "REMOTE LEASE FAILED" in capsys.readouterr().err
    assert _local_leases(tmp_path) == [], "an unreachable gateway produced a LOCAL lease"


def test_unset_means_local_mode(tmp_path, monkeypatch):
    monkeypatch.delenv(remote.ENV_URL, raising=False)
    assert remote.lease_url(config_reader=lambda key: "") is None
    monkeypatch.setenv(remote.ENV_URL, "")
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "local-data"))
    assert cli.main(["lease", "acquire", "a.py", "--actor", "me"]) == 0
    assert [lz.target for lz in _local_leases(tmp_path)] == ["a.py"]


def test_url_normalisation():
    assert remote.normalize_url("http://localhost:8182") == "http://127.0.0.1:8182/mcp"
    assert remote.normalize_url("https://gw.example/mcp") == "https://gw.example/mcp"
    with pytest.raises(remote.RemoteLeaseError):
        remote.normalize_url("gw.example:8182")
    assert remote.normalize_url("http://[::1]:8182") == "http://[::1]:8182/mcp"


@pytest.mark.parametrize("url", ["http://gw.example:8182", "http://192.168.1.5/mcp",
                                 "http://100.64.0.38:8182"])
def test_plain_http_to_a_non_loopback_host_is_refused(url):
    """Every request carries the session bearer; plain http off-box is cleartext."""
    with pytest.raises(remote.RemoteLeaseError, match="cleartext"):
        remote.normalize_url(url)


def test_a_redirect_is_never_followed_with_the_bearer():
    """urllib copies Authorization onto a redirect, cross-host included."""
    seen = []

    class Sink(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_POST(self):  # noqa: N802 -- http.server API
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        do_GET = do_POST  # noqa: N815 -- urllib turns a POST into a GET on 302

    sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    target = f"http://127.0.0.1:{sink.server_address[1]}/mcp"

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_POST(self):  # noqa: N802 -- http.server API
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

    src = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=src.serve_forever, daemon=True).start()
    try:
        client = remote.GatewayClient(f"http://127.0.0.1:{src.server_address[1]}",
                                      bearer="secret-bearer", timeout=5)
        with pytest.raises(remote.RemoteLeaseError, match="redirect"):
            client.call("vcs_lease_list", {})
        assert seen == [], "the bearer was forwarded to the redirect target"
    finally:
        for s in (src, sink):
            s.shutdown()
            s.server_close()


def test_sync_pull_and_push_through_the_gateway(ops_store, tmp_path, monkeypatch):
    gw = _Gateway(ops_store)  # the gateway holds the ops
    try:
        client = remote.GatewayClient(f"http://127.0.0.1:{gw.port}", bearer="b")
        full = mcp.vcs_sync_export(data_root=str(ops_store))
        monkeypatch.setattr(mcp, "SYNC_MAX_BUNDLE_BYTES", full["bytes"] - 1)
        node = tmp_path / "node-data"
        res = remote.pull(client, data_root=node)
        assert res["imported_ops"] == 3 and res["pages"] >= 2, res
        assert remote.pull(client, data_root=node)["imported_ops"] == 0

        # And back the other way, into an empty gateway.
        gw2 = _Gateway(tmp_path / "gw2")
        try:
            c2 = remote.GatewayClient(f"http://127.0.0.1:{gw2.port}", bearer="b")
            first = remote.push(c2, data_root=node)
            assert first["sent_ops"] == first["imported_ops"] == 3, first
            assert sync_status(tmp_path / "gw2")["ops"] == 3
            again = remote.push(c2, data_root=node)  # idempotent on the receiving side
            assert again["imported_ops"] == 0 and again["skipped"] == 3, again
            assert sync_status(tmp_path / "gw2")["ops"] == 3
        finally:
            gw2.close()
    finally:
        gw.close()


# ── review fixes: skew, paging cost, malformed pages, concurrent imports ────


def _bundle_of(ops_store) -> dict:
    return json.loads(base64.b64decode(
        mcp.vcs_sync_export(data_root=str(ops_store))["bundle"]))


class _FakeClient:
    """Answers vcs_sync_export with fixed pages; counts the calls."""

    url = "fake://gateway"

    def __init__(self, pages, import_reply=None):
        self.pages = list(pages)
        self.import_reply = import_reply
        self.calls = 0

    def call(self, tool, arguments):
        self.calls += 1
        if tool == "vcs_sync_import":
            return self.import_reply
        return self.pages[min(self.calls - 1, len(self.pages) - 1)]


def _page(bundle, complete=True):
    enc = mcp._encode_bundle(bundle)
    return {"bundle": enc, "bytes": len(enc), "op_count": len(bundle["ops"]),
            "complete": complete, "remaining": 0 if complete else 5}


def _from_newer_awgit(bundle):
    out = dict(bundle)
    out["ops"] = [dict(o, schema_version=999) for o in bundle["ops"]]
    return out


def test_import_counts_an_unreadable_op_as_rejected_not_skipped(ops_store, tmp_path):
    res = sync_mod.import_delta(_from_newer_awgit(_bundle_of(ops_store)),
                                data_root=tmp_path / "d")
    assert res["rejected_ops"] == 3 and res["skipped"] == 0 and res["imported_ops"] == 0


def test_pull_fails_when_every_op_is_from_a_newer_schema(ops_store, tmp_path):
    """(a) one complete page: this used to print 'pulled 0 ops' and exit 0."""
    client = _FakeClient([_page(_from_newer_awgit(_bundle_of(ops_store)))])
    with pytest.raises(remote.RemoteLeaseError, match="could not apply 3"):
        remote.pull(client, data_root=tmp_path / "node")


def test_pull_stops_at_once_on_a_page_it_cannot_apply(ops_store, tmp_path):
    """(b) paged: the same page was requested until max_pages (hours on a real store)."""
    client = _FakeClient([_page(_from_newer_awgit(_bundle_of(ops_store)), complete=False)])
    with pytest.raises(remote.RemoteLeaseError):
        remote.pull(client, data_root=tmp_path / "node", max_pages=25)
    assert client.calls == 1, f"pull retried a page that cannot apply {client.calls} times"


def test_pull_turns_a_malformed_page_into_a_remote_error(tmp_path):
    for bad in ("not base64 !!", base64.b64encode(b'{"schema":"awgit-sync-v1",'
                                                   b'"ops":[1],"bodies":{}}').decode()):
        client = _FakeClient([{"bundle": bad, "op_count": 1, "complete": True}])
        with pytest.raises(remote.RemoteLeaseError, match="malformed"):
            remote.pull(client, data_root=tmp_path / "node")


def test_push_fails_when_the_gateway_rejects_what_it_was_sent(ops_store):
    client = _FakeClient([], import_reply={"imported_ops": 0, "skipped": 0,
                                           "rejected_ops": 3, "rejected_bodies": 0})
    with pytest.raises(remote.RemoteLeaseError, match="rejected 3"):
        remote.push(client, data_root=ops_store)


def test_import_refuses_non_object_ops_and_oversized_dict_bundles(
        ops_store, tmp_path, monkeypatch):
    res = mcp.vcs_sync_import({"schema": "awgit-sync-v1", "ops": [1, "x"], "bodies": {}},
                              data_root=str(tmp_path / "d"))
    assert "objects" in res["error"] and res["imported_ops"] == 0
    monkeypatch.setattr(mcp, "SYNC_MAX_BUNDLE_BYTES", 100)
    res = mcp.vcs_sync_import(_bundle_of_unbounded(ops_store), data_root=str(tmp_path / "d"))
    assert "cap" in res["error"], "a dict bundle skipped the size cap"


def _bundle_of_unbounded(ops_store):
    return sync_mod.export_delta(set(), data_root=ops_store)


def test_export_page_reads_only_the_bodies_it_ships(ops_store, monkeypatch):
    """Each page used to export (read + encode) EVERY op still owed, then trim."""
    full = sync_mod.export_delta(set(), data_root=ops_store)
    first = full["ops"][0]
    one_op = mcp._bundle_prefix(full, 1)
    cap = len(mcp._encode_bundle(one_op)) + 4
    reads = []
    real_get = BodyStore.get
    monkeypatch.setattr(BodyStore, "get", lambda self, sha: reads.append(sha)
                        or real_get(self, sha))
    monkeypatch.setattr(mcp, "SYNC_MAX_BUNDLE_BYTES", cap)
    page = mcp.vcs_sync_export(data_root=str(ops_store))
    assert page["op_count"] == 1 and page["remaining"] == 2 and not page["complete"]
    assert len(page["bundle"]) <= cap
    got = json.loads(base64.b64decode(page["bundle"]))
    assert got["ops"][0]["op_id"] == first["op_id"]
    assert set(got["bodies"]) == set(one_op["bodies"])
    # Only the shipped op's bodies plus at most the next op's (the one that did
    # not fit); never the rest of the store.
    assert len(set(reads)) < full["body_count"], (
        f"read {len(set(reads))} of {full['body_count']} bodies for a one-op page")


@pytest.mark.parametrize("slack", [0, 1, 7, 40, 400])
def test_streamed_page_matches_the_exact_prefix(ops_store, slack):
    """The streamed size count picks the same prefix the real-encode search does."""
    full = sync_mod.export_delta(set(), data_root=ops_store)
    for n in (1, 2, 3):
        cap = len(mcp._encode_bundle(mcp._bundle_prefix(full, n))) + slack
        page, remaining = sync_mod.export_delta_page(set(), cap, data_root=ops_store)
        assert len(mcp._encode_bundle(page)) <= cap
        want, _ = mcp._page_bundle(full, cap)
        assert [o["op_id"] for o in page["ops"]] == [o["op_id"] for o in want["ops"]]
        assert remaining == 3 - len(page["ops"])


def test_concurrent_imports_do_not_lose_applied_ids(ops_store, tmp_path, monkeypatch):
    """sync.json is read-modify-written; two pushes at once used to drop updates."""
    import time

    full = _bundle_of(ops_store)
    singles = [mcp._bundle_prefix(dict(full, ops=[op]), 1) for op in full["ops"]]
    real_state = sync_mod.sync_state

    def slow_state(data_root=None):
        out = real_state(data_root)
        time.sleep(0.2)  # widen the read -> write window
        return out

    monkeypatch.setattr(sync_mod, "sync_state", slow_state)
    dst = tmp_path / "gw"
    threads = [threading.Thread(target=sync_mod.import_delta, args=(b,),
                                kwargs={"data_root": dst}) for b in singles]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    monkeypatch.setattr(sync_mod, "sync_state", real_state)
    assert len(real_state(dst)["applied_op_ids"]) == 3
    assert sync_status(dst)["missing"] == 0
