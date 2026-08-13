"""Minimal Python source parser for standalone awgit.

Replicates the chunk shape of ``lib.faculties.CodeGraph.parse_source_bytes``
that awgit's capture/diff/merge consume — a ``ParseResult.chunks`` list whose
members expose ``name``, ``chunk_type`` (a ``.value`` in
``function|class|method|module``), ``signature``, ``start_line`` (1-indexed),
``end_line`` (inclusive). Built on Python's ``ast`` — no AitherOS dependency.

Fidelity notes (each verified against the reference so node ids stay stable
across the monorepo and the standalone package):
- Method chunks are named ``Class.method``. The stable node id is keyed on
  ``(name, path, type)``, so an unqualified method name would mint a different
  id than the reference parser and break cross-repo id stability.
- The MODULE chunk is emitted only when the file has a docstring or top-level
  constant assignments (the reference skips empty module chunks, so an
  unconditional one would mint a spurious module node on every file and turn
  every capture into a module rewrite). Its range is ``1..1`` like the
  reference.
- Function/method signatures replicate ``get_signature``: positional args with
  annotations + return annotation, ``async def`` prefix; defaults, ``*args``/
  ``**kwargs`` and ``/``/``*`` separators are deliberately omitted (matching
  the reference) so a signature change flags the same way.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_MODULE_CONST_LIMIT = 60
_MODULE_VALUE_CHARS = 120


class ChunkType(Enum):
    MODULE = "module"
    CLASS = "class"
    METHOD = "method"
    FUNCTION = "function"


@dataclass
class Chunk:
    name: str
    chunk_type: ChunkType
    signature: str = ""
    start_line: int = 0
    end_line: int = 0


@dataclass
class ParseResult:
    chunks: List[Chunk] = field(default_factory=list)


def _get_signature(node: ast.AST) -> str:
    """Full function signature — mirrors CodeGraph's ``get_signature``."""
    args: List[str] = []
    for arg in node.args.args:
        arg_str = arg.arg
        if arg.annotation:
            try:
                arg_str += f": {ast.unparse(arg.annotation)}"
            except Exception as e:  # malformed annotation: keep the bare arg
                logger.debug("awgit parser: annotation unparse failed: %s", e)
        args.append(arg_str)
    returns = ""
    if node.returns:
        try:
            returns = f" -> {ast.unparse(node.returns)}"
        except Exception as e:  # malformed return annotation: omit it
            logger.debug("awgit parser: return annotation unparse failed: %s", e)
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({', '.join(args)}){returns}"


def _module_chunk(tree: ast.Module, path: str) -> Optional[Chunk]:
    """One chunk at module scope — ONLY when there is something to index."""
    docstring = ast.get_docstring(tree)
    consts: List[str] = []
    for node in ast.iter_child_nodes(tree):
        targets: List[str] = []
        value = None
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        if not targets or value is None:
            continue
        try:
            rendered = ast.unparse(value)
        except Exception as e:  # un-unparseable const value: skip it
            logger.debug("awgit parser: const unparse failed: %s", e)
            continue
        if len(rendered) > _MODULE_VALUE_CHARS:
            rendered = rendered[:_MODULE_VALUE_CHARS] + "..."
        for t in targets:
            if t.startswith("__"):
                continue
            consts.append(f"{t} = {rendered}")
        if len(consts) >= _MODULE_CONST_LIMIT:
            break
    if not docstring and not consts:
        return None
    name = Path(path).stem
    return Chunk(
        name=name,
        chunk_type=ChunkType.MODULE,
        signature=f"module {name}",
        start_line=1,
        end_line=1,
    )


def _class_chunk(node: ast.ClassDef) -> Chunk:
    bases: List[str] = []
    for b in node.bases:
        try:
            bases.append(ast.unparse(b))
        except Exception as e:  # un-unparseable base: omit it from the header
            logger.debug("awgit parser: class base unparse failed: %s", e)
    signature = f"class {node.name}"
    if bases:
        signature += f"({', '.join(bases)})"
    return Chunk(
        name=node.name,
        chunk_type=ChunkType.CLASS,
        signature=signature,
        start_line=node.lineno,
        end_line=node.end_lineno or node.lineno,
    )


def _func_chunk(node: ast.AST, chunk_type: ChunkType) -> Chunk:
    return Chunk(
        name=node.name,
        chunk_type=chunk_type,
        signature=_get_signature(node),
        start_line=node.lineno,
        end_line=node.end_lineno or node.lineno,
    )


def _method_chunk(class_name: str, node: ast.AST) -> Chunk:
    return Chunk(
        name=f"{class_name}.{node.name}",
        chunk_type=ChunkType.METHOD,
        signature=_get_signature(node),
        start_line=node.lineno,
        end_line=node.end_lineno or node.lineno,
    )


def parse_source_bytes(content: bytes, path: str) -> ParseResult:
    """Parse Python source bytes into chunks (no disk, no AitherOS deps).

    One chunk per top-level function, class, and class method, plus a module
    chunk when the file carries a docstring or top-level constants. Mirrors the
    reference extractor's traversal depth exactly (methods live one level below
    classes; nested classes/functions are not indexed).
    """
    # utf-8-SIG, not utf-8. A BOM decodes to U+FEFF under plain utf-8 and ast
    # rejects it, while Python's own loader imports the file fine — the same
    # asymmetry that made 11 shipped modules invisible to check_undefined_names.
    # Read as plain utf-8, every BOM-prefixed file would be classified
    # unparseable and silently skipped forever.
    source = content.decode("utf-8-sig", errors="ignore")
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return ParseResult(chunks=[])
    result = ParseResult()
    module = _module_chunk(tree, path)
    if module is not None:
        result.chunks.append(module)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            result.chunks.append(_class_chunk(node))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    result.chunks.append(_method_chunk(node.name, item))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result.chunks.append(_func_chunk(node, ChunkType.FUNCTION))
    return result
