"""Row-level identity and content addressing for tabular data.

awgit's thesis is *diff meaning, not lines*. For code that means a stable node id
per function plus a content sha for its body. A CSV row is meaning too, and a
line diff mangles it: sort the file and every line "changed"; add a column and
every line "changed"; reorder rows and a review drowns.

So this module applies awgit's own two-hash pair to table rows:

    row identity = H(chosen KEY columns)   -- which row is this?
    row content  = H(ALL columns)          -- has it changed?

A diff is then set algebra on identity: present in both with differing content is
MODIFIED, identity only on the left is REMOVED, only on the right is ADDED. Rows
can be reordered freely and nothing is reported, because identity does not depend
on position.

Adapted from Oxen (github.com/oxen-ai/Oxen, Apache-2.0), whose
``df_hash_rows_on_cols`` / ``df_hash_rows`` pair is the same idea on Polars. No
Oxen code is copied — this is a reimplementation on the stdlib, because awgit
ships to PyPI with one runtime dependency and Oxen is Rust on Polars/RocksDB.
See ``.RESEARCH/INTAKE/Oxen/DOSSIER.md``.

**These hashes are awgit's, not Oxen's.** The value-to-bytes encoding below is
deliberately different (see ``_encode``), so a row hash from this module will not
equal the one Oxen computes for the same row. Nothing interoperates across the
two tools; do not build anything that assumes it does.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: Column names used when a hashed table is materialised. Prefixed so they cannot
#: collide with a real column short of deliberate sabotage.
ROW_ID_COL = "_awgit_row_id"
ROW_SHA_COL = "_awgit_row_sha"

#: Suffixes routed to the tabular path. Everything else stays an opaque body.
#: This is Oxen's `tracking_type: tabular|regular` split — the switch that makes
#: row-level diffing reachable at all.
TABULAR_SUFFIXES = {".csv", ".tsv", ".parquet"}


def is_tabular(path: Path | str) -> bool:
    """True when this path should take the row-aware path rather than blob-diff."""
    return Path(path).suffix.lower() in TABULAR_SUFFIXES


def _encode(name: str, value: object) -> bytes:
    """Deterministic, UNAMBIGUOUS bytes for one cell.

    Length-prefixed on purpose. Oxen concatenates cell bytes into a buffer with
    no delimiter, which collides: the rows ``("ab", "c")`` and ``("a", "bc")``
    serialise identically and therefore hash identically, so a real edit can read
    as "unchanged". Prefixing each field's name and value with its byte length
    makes the encoding injective, which is the property a content address needs.

    ``None`` is distinguished from the empty string for the same reason — in CSV
    a missing column and an empty cell are different facts.
    """
    n = name.encode("utf-8")
    if value is None:
        v = b"\x00NULL"
    elif isinstance(value, bytes):
        v = value
    else:
        v = str(value).encode("utf-8")
    return b"%d:%s=%d:%s;" % (len(n), n, len(v), v)


def _digest(pairs: Iterable[Tuple[str, object]]) -> str:
    h = hashlib.sha256()
    for name, value in pairs:
        h.update(_encode(name, value))
    return h.hexdigest()


def row_content(row: Dict[str, object], columns: Sequence[str]) -> str:
    """Content address of the whole row — every column, in declared order."""
    return _digest((c, row.get(c)) for c in columns)


def row_identity(row: Dict[str, object], keys: Sequence[str]) -> Optional[str]:
    """Identity of the row from its KEY columns, or None when there are none.

    Returning None rather than falling back to hashing every column is the
    tie-break Oxen makes explicitly (`tabular.rs:918`, "to allow asymmetric
    target hashing for added / removed cols"). It matters: with no key, identity
    and content would be the same number, so every edit would look like an
    unrelated ADD plus an unrelated REMOVE while *claiming* to be a keyed diff.
    Signalling "no identity" lets the caller fall back honestly to a set diff.
    """
    if not keys:
        return None
    return _digest((k, row.get(k)) for k in keys)


@dataclass
class Table:
    columns: List[str]
    rows: List[Dict[str, object]]


@dataclass
class TableDiff:
    """A row-level diff. Counts are derived, never stored, so they cannot drift."""

    keys: List[str]
    added: List[Dict[str, object]] = field(default_factory=list)
    removed: List[Dict[str, object]] = field(default_factory=list)
    modified: List[Tuple[Dict[str, object], Dict[str, object]]] = field(default_factory=list)
    unchanged: int = 0
    #: Columns present on one side only — a schema change, which is a different
    #: fact from a row change and is reported separately rather than making every
    #: row look modified.
    columns_added: List[str] = field(default_factory=list)
    columns_removed: List[str] = field(default_factory=list)
    #: True when no key columns were supplied, so `modified` cannot be populated.
    keyless: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.modified
                    or self.columns_added or self.columns_removed)

    def summary(self) -> Dict[str, object]:
        return {
            "keys": self.keys,
            "keyless": self.keyless,
            "added": len(self.added),
            "removed": len(self.removed),
            "modified": len(self.modified),
            "unchanged": self.unchanged,
            "columns_added": self.columns_added,
            "columns_removed": self.columns_removed,
            "changed": self.changed,
        }


class UnreadableTableError(Exception):
    """Raised rather than returning an empty table.

    An unreadable file that reads as "0 rows" turns a parse failure into a diff
    claiming every row was deleted — the silent-empty class this repo keeps
    paying for. Callers must handle it; they must not be handed a plausible lie.
    """


def read_table(path: Path | str) -> Table:
    """Read a CSV/TSV with the stdlib, or a parquet via the optional extra."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return _read_parquet(path)
    delimiter = "\t" if suffix == ".tsv" else ","
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            reader = csv.DictReader(fh, delimiter=delimiter)
            columns = list(reader.fieldnames or [])
            rows = [dict(r) for r in reader]
    except OSError as exc:
        raise UnreadableTableError(f"{path}: {exc}") from exc
    if not columns:
        raise UnreadableTableError(f"{path}: no header row — cannot identify columns")
    return Table(columns=columns, rows=rows)


def _read_parquet(path: Path) -> Table:
    """Parquet lives behind an OPTIONAL extra (`pip install awgit[tabular]`).

    Kept optional deliberately: pyarrow is tens of megabytes and awgit's whole
    install is currently one small dependency. The absence is reported as a clear
    instruction, never as an empty table.
    """
    try:
        import pyarrow.parquet as pq  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise UnreadableTableError(
            f"{path}: reading parquet needs the optional extra — "
            f"`pip install awgit[tabular]`"
        ) from exc
    try:
        table = pq.read_table(path)
    except Exception as exc:  # noqa: BLE001 - pyarrow raises a wide family
        raise UnreadableTableError(f"{path}: {exc}") from exc
    columns = list(table.column_names)
    return Table(columns=columns, rows=table.to_pylist())


def diff_tables(old: Table, new: Table, keys: Sequence[str]) -> TableDiff:
    """Row-level diff, keyed on `keys` (order-insensitive across rows)."""
    keys = list(keys)
    out = TableDiff(keys=keys, keyless=not keys)
    out.columns_added = [c for c in new.columns if c not in old.columns]
    out.columns_removed = [c for c in old.columns if c not in new.columns]

    if not keys:
        # No identity: fall back to a CONTENT set diff. Honest about what it can
        # see — every change reads as an add plus a remove, and `modified` stays
        # empty rather than being guessed at.
        old_by_content: Dict[str, Dict[str, object]] = {
            row_content(r, old.columns): r for r in old.rows}
        new_by_content: Dict[str, Dict[str, object]] = {
            row_content(r, new.columns): r for r in new.rows}
        for sha, r in new_by_content.items():
            if sha not in old_by_content:
                out.added.append(r)
        for sha, r in old_by_content.items():
            if sha not in new_by_content:
                out.removed.append(r)
        out.unchanged = len(set(old_by_content) & set(new_by_content))
        return out

    missing = [k for k in keys if k not in old.columns and k not in new.columns]
    if missing:
        raise UnreadableTableError(
            f"key column(s) {missing} exist in neither table — a diff keyed on a "
            f"column that is not there would silently report every row as "
            f"added+removed")

    old_by_id = {row_identity(r, keys): r for r in old.rows}
    new_by_id = {row_identity(r, keys): r for r in new.rows}

    for rid, new_row in new_by_id.items():
        old_row = old_by_id.get(rid)
        if old_row is None:
            out.added.append(new_row)
            continue
        # Compare on the UNION of columns so a dropped/added column shows up as a
        # modification of the rows it touches, not as silence.
        cols = list(dict.fromkeys(list(old.columns) + list(new.columns)))
        if row_content(old_row, cols) != row_content(new_row, cols):
            out.modified.append((old_row, new_row))
        else:
            out.unchanged += 1

    for rid, old_row in old_by_id.items():
        if rid not in new_by_id:
            out.removed.append(old_row)
    return out


def diff_files(old_path: Path | str, new_path: Path | str,
               keys: Sequence[str]) -> TableDiff:
    return diff_tables(read_table(old_path), read_table(new_path), keys)
