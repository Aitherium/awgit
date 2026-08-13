"""Row-level tabular diff — the properties that make it worth having."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from awgit.tabular import (
    Table,
    UnreadableTableError,
    diff_files,
    diff_tables,
    is_tabular,
    row_content,
    row_identity,
)


def _csv(tmp_path: Path, name: str, columns, rows) -> Path:
    p = tmp_path / name
    with open(p, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return p


def _t(columns, rows) -> Table:
    return Table(columns=list(columns), rows=[dict(r) for r in rows])


# --- the encoding property everything else rests on -------------------------

def test_encoding_is_injective_across_field_boundaries():
    """Two different rows must never share a content address.

    The values below are chosen so that a naive `name + value` concatenation
    produces the SAME byte string for both rows — the second row's value absorbs
    the next column's name:

        x="ay", y="b"  -> "x" "ay" "y" "b"  -> xayyb
        x="a",  y="yb" -> "x" "a"  "y" "yb" -> xayyb

    Oxen appends cell bytes into a buffer with no delimiter at all, so it is
    vulnerable to this class; a real edit hashes as "unchanged". Length-prefixing
    each name and value makes the encoding injective.

    The first version of this test used ("ab","c") vs ("a","bc"), which does NOT
    collide once column names are interleaved — it passed even with the encoder
    deliberately broken. A mutation run caught it. An assertion that cannot fail
    is worse than no assertion, because it reads as coverage.
    """
    cols = ["x", "y"]
    a = row_content({"x": "ay", "y": "b"}, cols)
    b = row_content({"x": "a", "y": "yb"}, cols)
    assert a != b


def test_null_collides_with_nothing():
    """A null must differ from an empty cell AND from the literal text "None".

    The second half is the one that needs the sentinel. Without it, `None` falls
    through to `str(None)` and hashes as the four characters "None" — so a data
    cell whose value really is the string "None" (common in exported CSVs, and
    in any column of Python reprs) becomes indistinguishable from a missing
    value. A first version of this test asserted only `None != ""`, which passes
    even with the sentinel removed; a mutation run caught that it proved nothing.
    """
    cols = ["x"]
    null = row_content({"x": None}, cols)
    assert null != row_content({"x": ""}, cols)
    assert null != row_content({"x": "None"}, cols)


def test_content_is_order_stable_but_column_sensitive():
    assert row_content({"a": 1, "b": 2}, ["a", "b"]) == row_content({"b": 2, "a": 1}, ["a", "b"])
    assert row_content({"a": 1, "b": 2}, ["a", "b"]) != row_content({"a": 1, "b": 2}, ["b", "a"])


# --- identity ---------------------------------------------------------------

def test_identity_ignores_non_key_columns():
    keys = ["id"]
    assert row_identity({"id": "7", "v": "a"}, keys) == row_identity({"id": "7", "v": "b"}, keys)


def test_identity_is_none_without_keys():
    """The Oxen tie-break: no keys means NO identity, not identity==content.

    Collapsing the two would make every edit look like an unrelated add+remove
    while still claiming to be a keyed diff.
    """
    assert row_identity({"a": 1}, []) is None


# --- the diff ---------------------------------------------------------------

def test_reordering_rows_is_not_a_change():
    """The whole point. A line diff calls this a total rewrite."""
    old = _t(["id", "v"], [{"id": "1", "v": "a"}, {"id": "2", "v": "b"}])
    new = _t(["id", "v"], [{"id": "2", "v": "b"}, {"id": "1", "v": "a"}])
    d = diff_tables(old, new, ["id"])
    assert not d.changed
    assert d.unchanged == 2


def test_modified_row_is_modified_not_add_plus_remove():
    old = _t(["id", "v"], [{"id": "1", "v": "a"}])
    new = _t(["id", "v"], [{"id": "1", "v": "b"}])
    d = diff_tables(old, new, ["id"])
    assert len(d.modified) == 1 and not d.added and not d.removed
    before, after = d.modified[0]
    assert before["v"] == "a" and after["v"] == "b"


def test_add_and_remove_are_detected():
    old = _t(["id"], [{"id": "1"}, {"id": "2"}])
    new = _t(["id"], [{"id": "2"}, {"id": "3"}])
    d = diff_tables(old, new, ["id"])
    assert [r["id"] for r in d.added] == ["3"]
    assert [r["id"] for r in d.removed] == ["1"]
    assert d.unchanged == 1


def test_column_change_is_reported_separately_from_rows():
    """Adding a column must not make every row read as MODIFIED-for-no-reason...

    ...but it IS a change to the rows that gain a value, so it shows in both
    places: the schema delta names the column, and the affected rows appear as
    modified. Silence in either place would be the bug.
    """
    old = _t(["id"], [{"id": "1"}])
    new = _t(["id", "extra"], [{"id": "1", "extra": "x"}])
    d = diff_tables(old, new, ["id"])
    assert d.columns_added == ["extra"] and d.columns_removed == []
    assert len(d.modified) == 1


def test_keyless_diff_is_honest_about_what_it_cannot_see():
    """With no keys: content set diff, and `modified` stays EMPTY by design."""
    old = _t(["v"], [{"v": "a"}])
    new = _t(["v"], [{"v": "b"}])
    d = diff_tables(old, new, [])
    assert d.keyless is True
    assert len(d.added) == 1 and len(d.removed) == 1
    assert d.modified == []


def test_key_column_absent_from_both_sides_raises():
    """Rather than reporting every row added+removed, which looks like data loss."""
    old = _t(["a"], [{"a": "1"}])
    new = _t(["a"], [{"a": "2"}])
    with pytest.raises(UnreadableTableError):
        diff_tables(old, new, ["nope"])


# --- file level -------------------------------------------------------------

def test_diff_files_roundtrip(tmp_path: Path):
    old = _csv(tmp_path, "old.csv", ["id", "v"], [{"id": "1", "v": "a"},
                                                  {"id": "2", "v": "b"}])
    new = _csv(tmp_path, "new.csv", ["id", "v"], [{"id": "2", "v": "B"},
                                                  {"id": "3", "v": "c"}])
    d = diff_files(old, new, ["id"])
    s = d.summary()
    # id=1 gone -> removed; id=2 value changed -> modified; id=3 new -> added.
    assert (s["added"], s["removed"], s["modified"], s["unchanged"]) == (1, 1, 1, 0)
    assert d.added[0]["id"] == "3"
    assert d.removed[0]["id"] == "1"
    before, after = d.modified[0]
    assert before["v"] == "b" and after["v"] == "B"


def test_headerless_file_raises_rather_than_reporting_zero_rows(tmp_path: Path):
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    with pytest.raises(UnreadableTableError):
        diff_files(p, p, ["id"])


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(UnreadableTableError):
        diff_files(tmp_path / "nope.csv", tmp_path / "nope.csv", [])


def test_tracking_type_split():
    assert is_tabular("a/b/data.csv") and is_tabular("x.PARQUET") and is_tabular("t.tsv")
    assert not is_tabular("module.py") and not is_tabular("photo.png")


# --- the guards that prove the guards ---------------------------------------
#
# Every assertion above is only worth what it CATCHES. Two of them were vacuous
# when first written — they passed with the code deliberately broken — and that
# was found by mutating the module by hand, once, in a throwaway script. A guard
# that ran once is not a guard, so the mutants live here and run on every suite.
#
# This is the same shape as dev/tests/test_event_loop_offload_contract.py, whose
# every claim carries a mutation reproducing the old defect.

_MUTANTS = {
    # The encoding property. Reverting to Oxen's delimiter-free concatenation
    # must break the injectivity test.
    "naive concat (no length prefix)": (
        'return b"%d:%s=%d:%s;" % (len(n), n, len(v), v)',
        "return n + v",
    ),
    # The NULL sentinel. Without it None falls through to str(None) == "None".
    "null falls through to str()": (
        "if value is None:",
        "if False:",
    ),
    # Identity must read the KEY columns, not the whole row.
    "identity ignores the key list": (
        "return _digest((k, row.get(k)) for k in keys)",
        "return _digest((c, row.get(c)) for c in sorted(row))",
    ),
    # No keys must mean NO identity, not a constant one.
    "keyless fabricates an identity": (
        "if not keys:\n        return None",
        "if not keys:\n        return _digest([])",
    ),
    # A changed row must not be counted as unchanged.
    "modified counted as unchanged": (
        "out.modified.append((old_row, new_row))",
        "out.unchanged += 1",
    ),
}


@pytest.mark.parametrize("label", sorted(_MUTANTS))
def test_suite_catches_mutant(label: str, tmp_path: Path):
    """Break the module one way; the rest of this file must go red.

    Runs the suite in a SUBPROCESS against a mutated copy of the package, so the
    already-imported module in this process is untouched and the mutation cannot
    leak into another test.
    """
    import shutil
    import subprocess
    import sys

    import awgit.tabular as mod

    old, new = _MUTANTS[label]
    original = Path(mod.__file__).read_text(encoding="utf-8")
    assert old in original, (
        f"mutation anchor for {label!r} no longer matches the source — the "
        f"mutant silently became a no-op, which is how a mutation suite starts "
        f"proving nothing"
    )

    pkg_root = Path(mod.__file__).resolve().parents[1]
    sandbox = tmp_path / "sandbox"
    shutil.copytree(pkg_root, sandbox / "awgit",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(Path(__file__), sandbox / "test_tabular.py")
    (sandbox / "awgit" / "tabular.py").write_text(
        original.replace(old, new, 1), encoding="utf-8")

    # -k excludes THIS test, or the subprocess would recurse into itself.
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "test_tabular.py", "-q",
         "-k", "not catches_mutant", "-p", "no:cacheprovider"],
        cwd=sandbox, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    assert proc.returncode != 0, (
        f"MUTANT SURVIVED: {label}. The suite passed with the module broken this "
        f"way, so whichever assertion covers it is vacuous.\n{proc.stdout[-1500:]}"
    )
