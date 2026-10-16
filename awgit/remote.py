"""Remote coordination: leases and op-log sync through ONE MCP gateway.

WHY THIS EXISTS
---------------
``LeaseRegistry`` persists to a LOCAL file. Two agents on two machines hold two
files, both acquire the same target, and neither sees a conflict -- it does not
error, it silently grants both. A gateway that mounts awgit's ``vcs_*`` tools
with one data root is a single serialization point, so a lease taken THROUGH it
is real mutual exclusion across machines. This module is the CLI's way there.

**Opt-in, and loud once opted in.** Remote mode is on when ``AWGIT_LEASE_URL``
(or ``git config awgit.leaseUrl``) names a gateway; unset, the local file stays
the default. Once it is set, a gateway that cannot be reached, rejects the
bearer, or returns an error RAISES ``RemoteLeaseError``. There is no fallback
to the local file: a lease granted locally because the shared store was down is
exactly the silent double-grant this exists to end.

**Transport.** Plain MCP streamable HTTP (JSON-RPC: ``initialize``,
``notifications/initialized``, ``tools/call``), stdlib only. The bearer comes
from ``AITHER_MCP_BEARER`` or ``~/.aither/session-bearer`` and is never
printed. TLS verification is always on for ``https://``; a private CA is
supplied with ``AWGIT_CA_BUNDLE``, never by turning verification off.
``localhost`` is rewritten to ``127.0.0.1``: on hosts where ``localhost``
resolves to ``::1`` first, a v4-only gateway refuses only after a delay.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

ENV_URL = "AWGIT_LEASE_URL"
CONFIG_KEY = "awgit.leaseUrl"
ENV_BEARER = "AITHER_MCP_BEARER"
ENV_CA = "AWGIT_CA_BUNDLE"
BEARER_FILE = Path.home() / ".aither" / "session-bearer"
PROTOCOL = "2025-06-18"
_LEASE_ID = re.compile(r"^[0-9a-f]{32}$")


class RemoteLeaseError(RuntimeError):
    """The configured gateway could not answer. Never caught into a fallback."""


def _git_config(key: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "config", "--get", key], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def normalize_url(raw: str) -> str:
    """``localhost`` -> ``127.0.0.1``; a bare origin gets the ``/mcp`` path.

    ``http://`` is accepted for loopback only. Every request carries the
    session bearer, and plain HTTP to any other host sends it in cleartext.
    """
    parts = urllib.parse.urlsplit(raw.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RemoteLeaseError(
            f"{ENV_URL}={raw!r} is not an http(s) URL -- refusing to guess, "
            "and refusing to fall back to the local lease file")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
        raise RemoteLeaseError(
            f"{ENV_URL}={raw!r} is plain http to a non-loopback host; the session "
            "bearer would travel in cleartext. Use https:// (a private CA goes in "
            f"{ENV_CA})")
    netloc = parts.netloc
    if parts.hostname == "localhost":
        netloc = "127.0.0.1" + (f":{parts.port}" if parts.port else "")
    path = parts.path if parts.path not in ("", "/") else "/mcp"
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, parts.query, ""))


def lease_url(config_reader: Callable[[str], str] = _git_config) -> Optional[str]:
    """The configured gateway URL, or None for local mode.

    Env first (per-process, what an agent harness sets), then git config (per
    repository or user). An empty value means local mode, deliberately.
    """
    raw = os.environ.get(ENV_URL)
    if raw is None:
        raw = config_reader(CONFIG_KEY)
    raw = (raw or "").strip()
    return normalize_url(raw) if raw else None


def read_bearer() -> str:
    env = os.environ.get(ENV_BEARER)
    if env is not None:
        return env.strip()
    try:
        return BEARER_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _ssl_context() -> ssl.SSLContext:
    # Verification ON, always. A private CA is added, never a check removed.
    return ssl.create_default_context(cafile=os.environ.get(ENV_CA) or None)


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect. urllib copies ``Authorization`` onto the new
    request, including to another host or from https to http, so following one
    can hand the session bearer to whoever the gateway (or something in front of
    it) points at."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RemoteLeaseError(
            f"gateway {req.full_url} redirected (HTTP {code}) to {newurl!r}; "
            "refusing to follow it with the session bearer. Point "
            f"{ENV_URL} at the final URL.")


def _opener(url: str) -> urllib.request.OpenerDirector:
    handlers: List[Any] = [_RefuseRedirect()]
    if url.startswith("https://"):
        handlers.append(urllib.request.HTTPSHandler(context=_ssl_context()))
    return urllib.request.build_opener(*handlers)


class GatewayClient:
    """A minimal MCP streamable-HTTP client for the gateway's ``vcs_*`` tools."""

    def __init__(self, url: str, bearer: Optional[str] = None,
                 timeout: float = 30.0) -> None:
        self.url = normalize_url(url)
        self.bearer = read_bearer() if bearer is None else bearer
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self._next_id = 0

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": PROTOCOL}
        if self.bearer:
            h["Authorization"] = f"Bearer {self.bearer}"
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _post(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        req = urllib.request.Request(
            self.url, data=json.dumps(message).encode("utf-8"),
            headers=self._headers(), method="POST")
        try:
            resp = _opener(self.url).open(req, timeout=self.timeout)  # noqa: S310
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RemoteLeaseError(
                    f"gateway {self.url} rejected the bearer (HTTP {exc.code}); "
                    f"refresh {BEARER_FILE} or set {ENV_BEARER}") from exc
            raise RemoteLeaseError(
                f"gateway {self.url} answered HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RemoteLeaseError(
                f"gateway {self.url} unreachable ({exc}); {ENV_URL} is set, so "
                "awgit will NOT fall back to the local lease file -- unset it "
                "to work locally") from exc
        with resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            if "id" not in message or resp.status == 202:
                return None
            ctype = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read().decode("utf-8", errors="replace")
        if "text/event-stream" in ctype:
            return _sse_response(raw, message["id"])
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise RemoteLeaseError(f"gateway sent non-JSON: {raw[:200]!r}") from exc

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": f"awgit-{self._next_id}",
               "method": method, "params": params}
        resp = self._post(msg)
        if not isinstance(resp, dict):
            raise RemoteLeaseError(f"gateway gave no response to {method}")
        if "error" in resp:
            raise RemoteLeaseError(f"gateway {method} failed: {resp['error']}")
        return resp.get("result") or {}

    def connect(self) -> None:
        if self.session_id:
            return
        self._request("initialize", {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "awgit", "version": "1"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call one tool; return its JSON result or raise ``RemoteLeaseError``."""
        self.connect()
        result = self._request("tools/call", {"name": tool, "arguments": arguments})
        text = "".join(c.get("text", "") for c in result.get("content") or []
                       if isinstance(c, dict))
        if result.get("isError"):
            raise RemoteLeaseError(f"{tool} failed on the gateway: {text[:300]}")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            # Some servers wrap a non-object return as {"result": ...}.
            inner = structured.get("result")
            if set(structured) == {"result"} and isinstance(inner, dict):
                return inner
            return structured
        try:
            out = json.loads(text)
        except ValueError as exc:
            raise RemoteLeaseError(f"{tool} returned non-JSON: {text[:200]!r}") from exc
        if not isinstance(out, dict):
            raise RemoteLeaseError(f"{tool} returned {type(out).__name__}, not an object")
        return out


def _sse_response(raw: str, want_id: Any) -> Optional[Dict[str, Any]]:
    data: List[str] = []
    for line in raw.splitlines() + [""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
            continue
        if line == "" and data:
            try:
                msg = json.loads("\n".join(data))
            except ValueError:
                msg = None
            data = []
            if isinstance(msg, dict) and msg.get("id") == want_id:
                return msg
    raise RemoteLeaseError(f"gateway event stream ended without a reply to {want_id!r}")


# ── leases ──────────────────────────────────────────────────────────────────


def split_ids(raw: List[str]) -> Dict[str, List[str]]:
    """CLI arguments -> ``lease_ids`` (32-hex) and ``targets`` (anything else)."""
    ids = [a for a in raw if _LEASE_ID.match(a)]
    return {"lease_ids": ids, "targets": [a for a in raw if a not in ids]}


def acquire(client: GatewayClient, actor: str, targets: List[str],
            ttl_sec: int, reason: str) -> Dict[str, Any]:
    return client.call("vcs_lease_acquire", {
        "actor": actor, "targets": list(targets), "ttl_sec": int(ttl_sec),
        "reason": reason})


def heartbeat(client: GatewayClient, actor: str, raw: List[str]) -> Dict[str, Any]:
    return client.call("vcs_lease_heartbeat", {"actor": actor, **split_ids(raw)})


def release(client: GatewayClient, actor: str, raw: List[str]) -> Dict[str, Any]:
    return client.call("vcs_lease_release", {"actor": actor, **split_ids(raw)})


def list_leases(client: GatewayClient) -> Dict[str, Any]:
    return client.call("vcs_lease_list", {})


# ── op-log sync ─────────────────────────────────────────────────────────────


def pull(client: GatewayClient, data_root: Optional[Path] = None,
         max_pages: int = 1000) -> Dict[str, int]:
    """Import every op the gateway holds that this node lacks (paged, idempotent).

    Raises when the gateway sends an op or body this node cannot apply (for
    example an op from a newer awgit schema), or when a page leaves the applied
    set where it was. Either way the next request would ask for the same ops
    again, so carrying on would loop until ``max_pages`` and then report a
    misleading "did not converge". Stopping at once names the cause.
    """
    from awgit.mcp import decode_bundle
    from awgit.sync import import_delta, sync_state

    imported = pages = 0
    for _ in range(max_pages):
        known = sync_state(data_root)["applied_op_ids"]
        page = client.call("vcs_sync_export", {"known_op_ids": known})
        if page.get("error"):
            raise RemoteLeaseError(f"vcs_sync_export: {page['error']}")
        pages += 1
        if page.get("op_count"):
            try:
                bundle = decode_bundle(page.get("bundle"))
            except ValueError as exc:
                raise RemoteLeaseError(f"gateway sent a malformed sync page: {exc}") from exc
            res = import_delta(bundle, data_root=data_root)
            imported += int(res["imported_ops"])
            rejected = int(res.get("rejected_ops", 0))
            bad_bodies = int(res.get("rejected_bodies", 0))
            if rejected or bad_bodies:
                raise RemoteLeaseError(
                    f"sync pull: this node could not apply {rejected} op(s) and "
                    f"{bad_bodies} body(ies) from the gateway (an op from a newer "
                    "awgit schema, or corrupt data); imported "
                    f"{imported} op(s) before stopping. Upgrade awgit on this node.")
            grew = len(sync_state(data_root)["applied_op_ids"]) > len(known)
            if not grew and not page.get("complete", True):
                raise RemoteLeaseError(
                    f"sync pull: page {pages} applied nothing, so the next request "
                    "would ask for the same ops again; stopping")
        if page.get("complete", True) or not page.get("op_count"):
            return {"imported_ops": imported, "pages": pages}
    raise RemoteLeaseError(f"sync pull did not converge in {max_pages} pages")


def push(client: GatewayClient, data_root: Optional[Path] = None,
         known_op_ids: Optional[Set[str]] = None) -> Dict[str, Any]:
    """Send the gateway every op this node has that ``known_op_ids`` lacks.

    The gateway's import is idempotent, so the default (``known`` empty: send
    everything) is safe, only larger than it needs to be.

    Reports what the GATEWAY says it did (``imported_ops``, ``skipped``), not
    only what was sent. It raises when the gateway rejects an op or a body, or
    accounts for fewer ops than it was sent.
    """
    from awgit.mcp import SYNC_MAX_BUNDLE_BYTES, _encode_bundle
    from awgit.sync import export_delta_page

    known = set(known_op_ids or ())
    sent = imported = skipped = pages = 0
    while True:
        page, remaining = export_delta_page(known, SYNC_MAX_BUNDLE_BYTES,
                                            data_root=data_root)
        if not page["ops"]:
            if remaining:
                raise RemoteLeaseError("a single op exceeds the sync bundle cap")
            return {"sent_ops": sent, "imported_ops": imported,
                    "skipped": skipped, "pages": pages}
        res = client.call("vcs_sync_import", {"bundle": _encode_bundle(page)})
        if res.get("error"):
            raise RemoteLeaseError(f"vcs_sync_import: {res['error']}")
        pages += 1
        n = len(page["ops"])
        sent += n
        imported += int(res.get("imported_ops", 0))
        skipped += int(res.get("skipped", 0))
        rejected = int(res.get("rejected_ops", 0))
        bad_bodies = int(res.get("rejected_bodies", 0))
        accounted = int(res.get("imported_ops", 0)) + int(res.get("skipped", 0))
        if rejected or bad_bodies or accounted != n:
            raise RemoteLeaseError(
                f"sync push: the gateway applied {res.get('imported_ops', 0)} and "
                f"skipped {res.get('skipped', 0)} of {n} op(s) sent, and rejected "
                f"{rejected} op(s) and {bad_bodies} body(ies). It may run an "
                "older or newer awgit than this node.")
        known |= {o["op_id"] for o in page["ops"]}
