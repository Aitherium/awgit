"""Extension seam: behaviour a HOST application supplies that awgit must not contain.

awgit ships publicly and has to run on a stranger's machine with nothing but
``git`` and the standard library. Some behaviour it can usefully USE only exists
inside a larger system: a code-graph index that can compute a blast radius, a
richer multi-language parser, an accounting plane that provisions an identity,
a gate runner that can produce evidence for a review.

Forking the module to add that behaviour is what this seam replaces. Measured
2026-08-12, the monorepo's copy of awgit and the published copy had drifted in
**14 of 17** shared modules, and exactly TWO of those differences were real
behaviour — the blast-radius enrichment and the parser. Everything else was
re-worded prose that a human had re-sanitised by hand, once per file. A seam
turns "fork the file" into "register a function", so the two trees can be one
tree with an overlay.

Three properties are deliberate:

**A hook never raises into its caller.** A host that registers a broken
implementation degrades to the standalone behaviour. awgit sits on the commit
path; enrichment that cannot fail closed must fail *soft*, or a bad plugin
takes someone's commit with it.

**Registration is explicit.** awgit does NOT scan entry points at import.
A version-control tool must not execute third-party code as a side effect of
``import awgit`` — the host calls :func:`register` when it is ready.

**A missing hook is a normal state, not an error.** The default is the
standalone behaviour, which is always correct, merely less informed.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

# Hook names used by awgit itself. A host may register others; awgit ignores
# what it does not consume, so a newer host can target an older awgit.
PARSER = "parser"                # (bytes, str) -> graph with .chunks
BLAST_RADIUS = "blast_radius"    # (node_id, symbol, path) -> dict
EXTEND_PARSER = "extend_parser"  # (argparse subparsers action) -> None
GATES = "gates"                  # (list[str] changed paths) -> dict
THOUGHTS = "thoughts"            # (list[str] actors) -> dict

_hooks: Dict[str, Callable[..., Any]] = {}
_multi: Dict[str, List[Callable[..., Any]]] = {}


def register(name: str, fn: Callable[..., Any], *, multi: bool = False) -> None:
    """Register ``fn`` as the implementation of hook ``name``.

    ``multi=True`` appends instead of replacing, for hooks where several hosts
    may each contribute (``EXTEND_PARSER``). A single-valued hook registered
    twice keeps the LAST registration and says so, because silently ignoring
    the second would make load order decide behaviour invisibly.
    """
    if multi:
        _multi.setdefault(name, []).append(fn)
        return
    if name in _hooks and _hooks[name] is not fn:
        logger.debug("awgit.plugins: hook %r re-registered, replacing", name)
    _hooks[name] = fn


def unregister(name: str) -> None:
    """Drop hook ``name`` (both forms). Used by tests and by --self-test."""
    _hooks.pop(name, None)
    _multi.pop(name, None)


def registered(name: str) -> bool:
    """Whether anything is registered for ``name`` — for reporting, not control flow."""
    return name in _hooks or bool(_multi.get(name))


def names() -> List[str]:
    """Every registered hook name, sorted. Lets a host print what it wired."""
    return sorted(set(_hooks) | set(_multi))


def call(name: str, *args: Any, default: Any = None, **kwargs: Any) -> Any:
    """Call hook ``name``; return ``default`` if it is absent or it raises.

    The broad except is the contract, not laziness: the whole point is that an
    unavailable or broken enrichment degrades to standalone behaviour. It is
    logged at warning so a host can see its plugin is failing rather than
    quietly getting the default forever.
    """
    fn = _hooks.get(name)
    if fn is None:
        return default
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - see docstring; must not reach the caller
        logger.warning("awgit.plugins: hook %r failed (%s) — using default", name, exc)
        return default


def call_all(name: str, *args: Any, **kwargs: Any) -> List[Any]:
    """Call every implementation of a ``multi`` hook; skip the ones that raise."""
    out: List[Any] = []
    for fn in _multi.get(name, []):
        try:
            out.append(fn(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001 - one bad plugin must not stop the rest
            logger.warning("awgit.plugins: hook %r failed (%s) — skipped", name, exc)
    return out


def parse_source_bytes(src: bytes, rel_path: str) -> Any:
    """Parse ``src`` into a node graph, preferring a host-supplied parser.

    The host's parser is tried first and its result is only accepted if it
    actually looks like a graph. A parser that returns ``None`` is a silent
    "no nodes here", which capture would read as *every node was deleted* —
    the exact failure ``test_vcs_unparseable_is_not_deletion`` exists to
    prevent, so it must not be reachable through the seam either.
    """
    graph = call(PARSER, src, rel_path)
    if graph is not None and getattr(graph, "chunks", None) is not None:
        return graph
    from awgit.parser import parse_source_bytes as _builtin

    return _builtin(src, rel_path)


def blast_radius(node_id: str, symbol: str, path: str) -> Dict[str, Any]:
    """Impact enrichment for an escalated conflict; ``{}`` when unavailable.

    Standalone awgit has no code-graph index, so there is nothing to attach —
    the conflict record already carries the bodies and the symbol, which is
    what a resolver needs. A host with an index registers this hook.
    """
    out = call(BLAST_RADIUS, node_id, symbol, path, default={})
    return out if isinstance(out, dict) else {}


def extend_parser(subparsers: Any) -> None:
    """Let hosts add subcommands/flags to the awgit CLI."""
    call_all(EXTEND_PARSER, subparsers)


def thoughts(actors: List[str]) -> Dict[str, Any]:
    """Reasoning-trace enrichment for an evidence report; ``{}`` when absent.

    Standalone awgit records WHAT changed and WHO changed it. A host that also
    keeps the agents' reasoning can answer WHY, and that is the one question an
    op-log structurally cannot. Absent here means the evidence report simply
    does not mention reasoning — never a zero, which would assert that these
    agents did their work without any.

    The hook is handed only the actor labels already in the report; it must not
    be given anything that would make an evidence report depend on a host being
    present to be correct.
    """
    out = call(THOUGHTS, list(actors), default={})
    return out if isinstance(out, dict) else {}


def self_test() -> int:
    """Prove the seam still behaves: default, override, and failure-is-soft."""
    failures = 0
    sentinel = object()
    unregister("__selftest__")

    if call("__selftest__", default=sentinel) is not sentinel:
        print("plugins self-test: absent hook did not return the default")
        failures += 1

    register("__selftest__", lambda: "live")
    if call("__selftest__") != "live":
        print("plugins self-test: registered hook was not called")
        failures += 1

    def _boom() -> str:
        raise RuntimeError("plugin is broken")

    register("__selftest__", _boom)
    if call("__selftest__", default="fellback") != "fellback":
        print("plugins self-test: a raising hook did not degrade to the default")
        failures += 1
    unregister("__selftest__")

    register("__selftest_multi__", lambda: 1, multi=True)
    register("__selftest_multi__", _boom, multi=True)
    register("__selftest_multi__", lambda: 3, multi=True)
    if call_all("__selftest_multi__") != [1, 3]:
        print("plugins self-test: one raising multi-hook stopped the others")
        failures += 1
    unregister("__selftest_multi__")

    print("plugins self-test:", "OK" if not failures else f"{failures} FAILED")
    return 1 if failures else 0
