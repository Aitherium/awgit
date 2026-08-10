"""Multi-language node identity must keep working, or it fails SILENTLY.

The whole multilang path degrades to "no symbols" on any error — deliberately,
so one unparseable file cannot lose a commit's op. That safety property is also
what makes a regression invisible: if the adapter breaks, every non-Python file
simply stops producing node changes, which looks exactly like "these files have
no code in them". Nothing raises, nothing logs, and the graph just gets quieter.

So these tests assert the POSITIVE: real symbols come back, and their ids are
stable under movement. Stability under movement is the property node-level merge
rests on — a TypeScript function that moves position must keep its id, or every
reorder reads as delete+add and merge becomes worthless.

Skipped (not failed) when the optional parser is absent: it is an extra, and
awgit must remain installable and useful without it.
"""
from __future__ import annotations

import pytest
from awgit.repowise_parser import available, language_for, parse_symbols

pytestmark = pytest.mark.skipif(
    not available()[0], reason="repowise not installed (optional 'multilang' extra)"
)

TS_V1 = (
    "export function alpha(a: number) { return a + 1; }\n"
    "export function beta(b: string) { return b.trim(); }\n"
)
# beta MOVED to the top and alpha's BODY changed — the exact shape a reorder
# plus an edit takes in review, and the case a line-based tool cannot follow.
TS_V2 = (
    "export function beta(b: string) { return b.trim(); }\n"
    "export function alpha(a: number) { return a + 2; }\n"
)


def _by_symbol(content: str, path: str) -> dict:
    return {s["symbol"]: s for s in parse_symbols(content.encode(), path)}


def test_typescript_yields_symbols():
    syms = _by_symbol(TS_V1, "src/probe.ts")
    assert syms, "no symbols from TypeScript — the adapter is inert"
    assert any("alpha" in name for name in syms), sorted(syms)


def test_node_id_survives_a_move_and_a_body_change():
    a = _by_symbol(TS_V1, "src/probe.ts")
    b = _by_symbol(TS_V2, "src/probe.ts")
    shared = set(a) & set(b)
    assert shared, f"no symbol common to both versions: {sorted(a)} vs {sorted(b)}"
    for name in shared:
        assert a[name]["node_id"] == b[name]["node_id"], (
            f"{name} changed id across a move — node-level merge would see "
            f"delete+add instead of an edit"
        )


def test_nested_symbols_are_addressable():
    syms = parse_symbols(
        b"export class Widget { render() { return null; } }\n", "src/w.tsx")
    ids = [s["node_id"] for s in syms]
    assert any("Widget" in i for i in ids), ids
    # A method must be its OWN node, not folded into the class: merge operates
    # per node, and a class-sized node would collide on every method edit.
    assert len(ids) >= 2, f"expected class AND method nodes, got {ids}"


def test_documents_are_not_treated_as_code():
    # Admitting these made capture parse every doc commit to produce zero
    # symbols, and made "no op" ambiguous between a doc change and a miss.
    for path in ("README.md", "config.yaml", "package.json", "notes.toml"):
        assert language_for(path) is None, f"{path} should not claim a code language"


def test_unparseable_input_degrades_to_empty_not_an_exception():
    assert parse_symbols(b"", "src/probe.ts") == []
    assert parse_symbols(b"\x00\x01\x02 not source at all", "src/probe.ts") == []
    assert parse_symbols(b"whatever", "file.unknownextension") == []
