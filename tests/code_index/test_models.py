"""Tests for code_index.models."""

from __future__ import annotations

import pytest

from gobby.code_index.models import (
    CODE_INDEX_UUID_NAMESPACE,
    IndexedFile,
    IndexResult,
    Symbol,
)

pytestmark = pytest.mark.unit


# ── Symbol.make_id ──────────────────────────────────────────────────────


def test_symbol_make_id_deterministic() -> None:
    """Same inputs always produce the same UUID5."""
    id1 = Symbol.make_id("proj", "file.py", "foo", "function", 10)
    id2 = Symbol.make_id("proj", "file.py", "foo", "function", 10)
    assert id1 == id2


def test_symbol_make_id_varies_with_inputs() -> None:
    """Different inputs produce different IDs."""
    id_a = Symbol.make_id("proj", "file.py", "foo", "function", 10)
    id_b = Symbol.make_id("proj", "file.py", "foo", "function", 20)
    id_c = Symbol.make_id("proj", "file.py", "bar", "function", 10)
    assert id_a != id_b
    assert id_a != id_c


def test_symbol_make_id_is_valid_uuid() -> None:
    """Returned string is a valid UUID."""
    import uuid

    sid = Symbol.make_id("p", "f.py", "x", "function", 0)
    parsed = uuid.UUID(sid)
    assert parsed.version == 5


# ── Symbol construction & round-trip ────────────────────────────────────


def test_symbol_to_dict_round_trip(sample_symbols: list[Symbol]) -> None:
    """to_dict contains all fields and values match."""
    sym = sample_symbols[0]
    d = sym.to_dict()

    assert d["id"] == sym.id
    assert d["name"] == sym.name
    assert d["kind"] == sym.kind
    assert d["file_path"] == sym.file_path
    assert d["byte_start"] == sym.byte_start
    assert d["byte_end"] == sym.byte_end
    assert d["line_start"] == sym.line_start
    assert d["line_end"] == sym.line_end
    assert d["signature"] == sym.signature
    assert d["docstring"] == sym.docstring
    assert d["content_hash"] == sym.content_hash
    assert d["parent_symbol_id"] is None


def test_symbol_timestamps_auto_set() -> None:
    """created_at and updated_at are set automatically."""
    sym = Symbol(
        id="test-id",
        project_id="p",
        file_path="f.py",
        name="x",
        qualified_name="x",
        kind="function",
        language="python",
        byte_start=0,
        byte_end=10,
        line_start=1,
        line_end=1,
        content_hash="h",
    )
    assert sym.created_at != ""
    assert sym.updated_at != ""


def test_symbol_parent_in_dict(sample_symbols: list[Symbol]) -> None:
    """Method symbol includes parent_symbol_id in dict."""
    method = sample_symbols[2]  # add method
    d = method.to_dict()
    assert d["parent_symbol_id"] == sample_symbols[1].id  # Calculator class


# ── IndexedFile.make_id ─────────────────────────────────────────────────


def test_indexed_file_make_id_deterministic() -> None:
    """Same project+path -> same ID."""
    id1 = IndexedFile.make_id("proj", "src/main.py")
    id2 = IndexedFile.make_id("proj", "src/main.py")
    assert id1 == id2


def test_indexed_file_make_id_varies() -> None:
    """Different paths -> different IDs."""
    id1 = IndexedFile.make_id("proj", "a.py")
    id2 = IndexedFile.make_id("proj", "b.py")
    assert id1 != id2


def test_indexed_file_to_dict() -> None:
    """IndexedFile.to_dict returns expected fields."""
    f = IndexedFile(
        id="f-id",
        project_id="proj",
        file_path="src/lib.py",
        language="python",
        content_hash="abc",
        symbol_count=5,
        byte_size=1234,
    )
    d = f.to_dict()
    assert d["id"] == "f-id"
    assert d["language"] == "python"
    assert d["symbol_count"] == 5
    assert d["byte_size"] == 1234
    assert d["indexed_at"] != ""


# ── IndexResult ─────────────────────────────────────────────────────────


def test_index_result_to_dict() -> None:
    """IndexResult.to_dict contains all counters."""
    r = IndexResult(
        project_id="proj",
        files_indexed=10,
        files_skipped=3,
        symbols_found=42,
        duration_ms=500,
        errors=["bad file"],
    )
    d = r.to_dict()
    assert d["project_id"] == "proj"
    assert d["files_indexed"] == 10
    assert d["files_skipped"] == 3
    assert d["symbols_found"] == 42
    assert d["duration_ms"] == 500
    assert d["errors"] == ["bad file"]
    assert d["symbols_embedded"] == 0
    assert d["relationships_added"] == 0


def test_index_result_defaults() -> None:
    """IndexResult defaults are zero/empty."""
    r = IndexResult(project_id="p")
    assert r.files_indexed == 0
    assert r.files_skipped == 0
    assert r.symbols_found == 0
    assert r.errors == []
