"""Multi-language node identity for awgit, borrowed from repowise.

WHY (2026-08-09). awgit's node model is Python-only: ``parse_source_bytes``
routes to CPython's ``ast``, so every `.ts`, `.tsx`, `.go`, `.cs` file is
invisible to the diff, the merge engine and the graph. Measured on this repo,
that is not a corner case — repowise's index holds 20,451 files, of which
**7,824 are Python and 12,600+ are not** (2,859 `.tsx`, 2,163 `.ts`, 1,423
yaml, 1,015 json, …). The lease gate was widened to guard those files, but a
lease is all awgit could offer them: no node identity, nothing to merge,
nothing to draw.

Rather than grow a second tree-sitter stack inside awgit, this adapts the one
the platform already runs. repowise (PyPI, third-party) parses **75 extensions
across 20+ languages** and — the fact that decides it — emits symbol ids of the
form ``path::qualified_name``, which are STABLE. Verified before writing any of
this: a TypeScript function that MOVED position and had its BODY changed kept
the id ``probe.ts::alpha`` across both edits. Stability under movement is the
whole basis of node-level merge; without it this would be worthless.

Deliberately a THIN adapter:
  * Python still goes to CodeGraph. It is the semantics awgit was built on, its
    node ids are already in the op-log, and switching would orphan every op
    recorded so far.
  * repowise is OPTIONAL. It is a third-party dependency and the standalone
    package must not hard-require it — `available()` is false when it is absent
    and callers fall back, rather than awgit failing to import.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

_PARSER = None
_UNAVAILABLE: Optional[str] = None


def available() -> Tuple[bool, str]:
    """(usable, why-not). Never raises: absence is a fallback, not a failure."""
    global _PARSER, _UNAVAILABLE
    if _PARSER is not None:
        return True, ""
    if _UNAVAILABLE is not None:
        return False, _UNAVAILABLE
    try:
        from repowise.core.ingestion import ASTParser  # noqa: PLC0415
        _PARSER = ASTParser()
        return True, ""
    except Exception as exc:
        _UNAVAILABLE = f"repowise unavailable ({type(exc).__name__})"
        return False, _UNAVAILABLE


# Languages repowise names but which carry no CODE NODES — documents, data and
# markup. Admitting them made capture parse every .md/.json/.yaml commit to
# produce zero symbols, and worse, made "no op recorded" ambiguous: a doc commit
# and a genuinely-missed code commit looked identical, which is how a coverage
# figure of 47% got misread as half the commits being dropped.
SYMBOL_LESS_LANGUAGES = {
    "markdown", "asciidoc", "json", "yaml", "toml", "ini", "csv", "text",
    "html", "css", "xml", "sql",
}


def language_for(path: str) -> Optional[str]:
    """repowise's language for a path, or None when it does not handle it."""
    ok, _ = available()
    if not ok:
        return None
    try:
        from repowise.core.ingestion import EXTENSION_TO_LANGUAGE  # noqa: PLC0415
    except Exception:
        return None
    _, _, ext = (path or "").rpartition(".")
    if not ext:
        return None
    lang = EXTENSION_TO_LANGUAGE.get("." + ext.lower())
    return None if lang in SYMBOL_LESS_LANGUAGES else lang


def parse_symbols(content: bytes, path: str) -> List[dict]:
    """Symbols in `content` as awgit-shaped dicts. [] when it cannot parse.

    Returns node_id/symbol/kind/start_line/end_line/language. The node_id is
    repowise's own ``path::qualified_name``, NOT a hash of the body — that is
    precisely why it survives a move, and why it can be compared across two
    commits the way awgit's Python node ids are.
    """
    ok, _ = available()
    if not ok:
        return []
    lang = language_for(path)
    if not lang:
        return []
    try:
        from repowise.core.ingestion import FileInfo  # noqa: PLC0415

        info = FileInfo(
            path=path, abs_path=path, language=lang, size_bytes=len(content),
            git_hash="", last_modified=0.0, is_test=False, is_config=False,
            is_api_contract=False, is_entry_point=False,
        )
        parsed = _PARSER.parse_file(info, content)
    except Exception:
        # A file repowise chokes on must degrade to "no symbols", never take
        # the capture down: awgit records what it can see, and a parser crash
        # on one file is not a reason to lose the whole commit's op.
        return []

    out = []
    for sym in (getattr(parsed, "symbols", None) or []):
        nid = getattr(sym, "id", None)
        if not nid:
            continue
        out.append({
            "node_id": str(nid),
            "symbol": getattr(sym, "qualified_name", None) or getattr(sym, "name", ""),
            "kind": str(getattr(sym, "kind", "") or ""),
            "start_line": getattr(sym, "start_line", 0),
            "end_line": getattr(sym, "end_line", 0),
            "language": lang,
        })
    return out
