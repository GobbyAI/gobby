"""Tests for code_index.storage CRUD operations."""

from __future__ import annotations

import pytest

from gobby.code_index.models import (
    CallRelation,
    ContentChunk,
    ImportRelation,
    IndexedFile,
    IndexedProject,
    Symbol,
)
from gobby.code_index.storage import CodeIndexStorage

pytestmark = pytest.mark.unit


# ── Symbols ─────────────────────────────────────────────────────────────


def test_upsert_and_get_symbol(
    code_storage: CodeIndexStorage, sample_symbols: list[Symbol]
) -> None:
    """Round-trip: upsert then retrieve by ID."""
    sym = sample_symbols[0]
    code_storage.upsert_symbols([sym])

    retrieved = code_storage.get_symbol(sym.id)
    assert retrieved is not None
    assert retrieved.id == sym.id
    assert retrieved.name == sym.name
    assert retrieved.kind == sym.kind
    assert retrieved.content_hash == sym.content_hash


def test_upsert_symbols_returns_count(
    code_storage: CodeIndexStorage, sample_symbols: list[Symbol]
) -> None:
    """upsert_symbols returns the number of rows upserted."""
    count = code_storage.upsert_symbols(sample_symbols)
    assert count == len(sample_symbols)


def test_upsert_symbols_empty_list(code_storage: CodeIndexStorage) -> None:
    """Empty list returns 0."""
    assert code_storage.upsert_symbols([]) == 0


def test_upsert_symbols_update_on_conflict(
    code_storage: CodeIndexStorage, sample_symbols: list[Symbol]
) -> None:
    """Upserting the same symbol updates it instead of failing."""
    sym = sample_symbols[0]
    code_storage.upsert_symbols([sym])

    sym.signature = "def greet(name: str, greeting: str) -> str:"
    code_storage.upsert_symbols([sym])

    retrieved = code_storage.get_symbol(sym.id)
    assert retrieved is not None
    assert retrieved.signature == sym.signature


def test_get_symbol_not_found(code_storage: CodeIndexStorage) -> None:
    """Non-existent symbol returns None."""
    assert code_storage.get_symbol("nonexistent-id") is None


def test_get_symbols_for_file(code_storage: CodeIndexStorage, sample_symbols: list[Symbol]) -> None:
    """Retrieve all symbols for a specific file."""
    code_storage.upsert_symbols(sample_symbols)

    symbols = code_storage.get_symbols_for_file("proj-1", "src/app.py")
    assert len(symbols) == 3
    # Should be ordered by line_start
    assert symbols[0].line_start <= symbols[1].line_start


def test_get_symbols_for_file_empty(code_storage: CodeIndexStorage) -> None:
    """No symbols for a non-indexed file."""
    symbols = code_storage.get_symbols_for_file("proj-1", "missing.py")
    assert symbols == []


def test_search_symbols_by_name(
    code_storage: CodeIndexStorage, sample_symbols: list[Symbol]
) -> None:
    """Search finds symbols by name substring."""
    code_storage.upsert_symbols(sample_symbols)

    results = code_storage.search_symbols_by_name("greet", "proj-1")
    assert len(results) >= 1
    assert any(s.name == "greet" for s in results)


def test_search_symbols_by_name_with_kind_filter(
    code_storage: CodeIndexStorage, sample_symbols: list[Symbol]
) -> None:
    """Kind filter narrows search results."""
    code_storage.upsert_symbols(sample_symbols)

    # Search for all names, but only classes
    results = code_storage.search_symbols_by_name("Calc", "proj-1", kind="class")
    assert len(results) == 1
    assert results[0].kind == "class"


def test_search_symbols_by_qualified_name(
    code_storage: CodeIndexStorage, sample_symbols: list[Symbol]
) -> None:
    """Search matches qualified_name too (e.g., Calculator.add)."""
    code_storage.upsert_symbols(sample_symbols)

    results = code_storage.search_symbols_by_name("Calculator.add", "proj-1")
    assert len(results) >= 1
    assert any(s.qualified_name == "Calculator.add" for s in results)


def test_get_calls_for_file_round_trips_optional_fields_as_none(
    code_storage: CodeIndexStorage,
) -> None:
    """Optional callee metadata should round-trip as None, not empty strings."""
    calls = [
        CallRelation(
            caller_symbol_id="sym-1",
            callee_name="missing_target",
            file_path="src/app.py",
            line=42,
        )
    ]

    code_storage.upsert_calls("proj-1", "src/app.py", calls)

    results = code_storage.get_calls_for_file("proj-1", "src/app.py")
    assert results == [
        {
            "caller_symbol_id": "sym-1",
            "callee_symbol_id": None,
            "callee_name": "missing_target",
            "callee_target_kind": "unresolved",
            "callee_external_module": None,
            "file_path": "src/app.py",
            "line": 42,
        }
    ]


def test_delete_symbols_for_file(
    code_storage: CodeIndexStorage, sample_symbols: list[Symbol]
) -> None:
    """Delete all symbols for a file."""
    code_storage.upsert_symbols(sample_symbols)

    deleted = code_storage.delete_symbols_for_file("proj-1", "src/app.py")
    assert deleted == 3

    remaining = code_storage.get_symbols_for_file("proj-1", "src/app.py")
    assert remaining == []


def test_delete_symbols_for_file_returns_zero_for_missing(
    code_storage: CodeIndexStorage,
) -> None:
    """Deleting from a non-existent file returns 0."""
    assert code_storage.delete_symbols_for_file("proj-1", "gone.py") == 0


# ── Files ───────────────────────────────────────────────────────────────


def test_upsert_and_get_file(code_storage: CodeIndexStorage) -> None:
    """Round-trip: upsert then retrieve file record."""
    f = IndexedFile(
        id=IndexedFile.make_id("proj-1", "src/lib.py"),
        project_id="proj-1",
        file_path="src/lib.py",
        language="python",
        content_hash="hash123",
        symbol_count=5,
        byte_size=2048,
    )
    code_storage.upsert_file(f)

    retrieved = code_storage.get_file("proj-1", "src/lib.py")
    assert retrieved is not None
    assert retrieved.file_path == "src/lib.py"
    assert retrieved.content_hash == "hash123"
    assert retrieved.symbol_count == 5


def test_get_file_not_found(code_storage: CodeIndexStorage) -> None:
    """Missing file returns None."""
    assert code_storage.get_file("proj-1", "nope.py") is None


def test_list_files(code_storage: CodeIndexStorage) -> None:
    """List all indexed files for a project."""
    for name in ("a.py", "b.py", "c.py"):
        code_storage.upsert_file(
            IndexedFile(
                id=IndexedFile.make_id("proj-1", name),
                project_id="proj-1",
                file_path=name,
                language="python",
                content_hash=f"hash-{name}",
            )
        )

    files = code_storage.list_files("proj-1")
    assert len(files) == 3
    # Ordered by file_path
    assert files[0].file_path == "a.py"


def test_get_stale_files(code_storage: CodeIndexStorage) -> None:
    """Detect stale files whose hash has changed."""
    # Store a file with hash "old"
    code_storage.upsert_file(
        IndexedFile(
            id=IndexedFile.make_id("proj-1", "changed.py"),
            project_id="proj-1",
            file_path="changed.py",
            language="python",
            content_hash="old-hash",
        )
    )
    code_storage.upsert_file(
        IndexedFile(
            id=IndexedFile.make_id("proj-1", "same.py"),
            project_id="proj-1",
            file_path="same.py",
            language="python",
            content_hash="current-hash",
        )
    )

    current_hashes = {
        "changed.py": "new-hash",  # Changed
        "same.py": "current-hash",  # Unchanged
        "brand_new.py": "fresh-hash",  # New file
    }
    stale = code_storage.get_stale_files("proj-1", current_hashes)
    assert "changed.py" in stale
    assert "brand_new.py" in stale
    assert "same.py" not in stale


# ── Projects ────────────────────────────────────────────────────────────


def test_upsert_and_get_project_stats(code_storage: CodeIndexStorage) -> None:
    """Round-trip project statistics."""
    project = IndexedProject(
        id="proj-1",
        root_path="/home/user/project",
        total_files=20,
        total_symbols=150,
        last_indexed_at="2025-01-01T00:00:00",
        index_duration_ms=1200,
    )
    code_storage.upsert_project_stats(project)

    retrieved = code_storage.get_project_stats("proj-1")
    assert retrieved is not None
    assert retrieved.root_path == "/home/user/project"
    assert retrieved.total_files == 20
    assert retrieved.total_symbols == 150
    assert retrieved.index_duration_ms == 1200


def test_get_project_stats_not_found(code_storage: CodeIndexStorage) -> None:
    """Non-existent project returns None."""
    assert code_storage.get_project_stats("missing") is None


def test_upsert_project_stats_updates(code_storage: CodeIndexStorage) -> None:
    """Second upsert updates existing project stats."""
    project = IndexedProject(
        id="proj-1",
        root_path="/home/user/project",
        total_files=10,
        total_symbols=50,
    )
    code_storage.upsert_project_stats(project)

    project.total_files = 20
    project.total_symbols = 100
    code_storage.upsert_project_stats(project)

    retrieved = code_storage.get_project_stats("proj-1")
    assert retrieved is not None
    assert retrieved.total_files == 20
    assert retrieved.total_symbols == 100


def test_delete_project_index_removes_all_project_data(
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
) -> None:
    """Deleting a project index removes stats and all derived index records."""
    code_storage.upsert_project_stats(
        IndexedProject(
            id="proj-1",
            root_path="/home/user/project",
            total_files=1,
            total_symbols=len(sample_symbols),
        )
    )
    code_storage.upsert_file(
        IndexedFile(
            id=IndexedFile.make_id("proj-1", "src/app.py"),
            project_id="proj-1",
            file_path="src/app.py",
            language="python",
            content_hash="abc123",
            symbol_count=len(sample_symbols),
        )
    )
    code_storage.upsert_symbols(sample_symbols)
    code_storage.upsert_imports(
        "proj-1",
        "src/app.py",
        [ImportRelation(source_file="src/app.py", target_module="pathlib")],
    )
    code_storage.upsert_calls(
        "proj-1",
        "src/app.py",
        [
            CallRelation(
                caller_symbol_id=sample_symbols[0].id,
                callee_name="Path",
                file_path="src/app.py",
                line=3,
            )
        ],
    )
    code_storage.upsert_content_chunks(_make_chunks())

    counts = code_storage.delete_project_index("proj-1")

    assert counts == {
        "symbols": len(sample_symbols),
        "files": 1,
        "imports": 1,
        "calls": 1,
        "content_chunks": 2,
        "projects": 1,
    }
    assert code_storage.get_project_stats("proj-1") is None
    assert code_storage.list_files("proj-1") == []
    assert code_storage.count_symbols("proj-1") == 0
    assert code_storage.get_imports_for_file("proj-1", "src/app.py") == []
    assert code_storage.get_calls_for_file("proj-1", "src/app.py") == []
    assert code_storage.search_content_fts("def", "proj-1") == []


# ── Summaries ───────────────────────────────────────────────────────────


# ── Counts ──────────────────────────────────────────────────────────────


def test_count_symbols(code_storage: CodeIndexStorage, sample_symbols: list[Symbol]) -> None:
    """Count symbols for a project."""
    code_storage.upsert_symbols(sample_symbols)
    assert code_storage.count_symbols("proj-1") == 3


def test_count_files(code_storage: CodeIndexStorage) -> None:
    """Count indexed files for a project."""
    for name in ("a.py", "b.py"):
        code_storage.upsert_file(
            IndexedFile(
                id=IndexedFile.make_id("proj-1", name),
                project_id="proj-1",
                file_path=name,
                language="python",
                content_hash=f"h-{name}",
            )
        )
    assert code_storage.count_files("proj-1") == 2


# ── Content Chunks ─────────────────────────────────────────────────────


def _make_chunks(project_id: str = "proj-1", file_path: str = "src/app.py") -> list[ContentChunk]:
    """Helper to create sample content chunks."""
    return [
        ContentChunk(
            id=ContentChunk.make_id(project_id, file_path, 0),
            project_id=project_id,
            file_path=file_path,
            chunk_index=0,
            line_start=1,
            line_end=100,
            content='import os\nfrom pathlib import Path\n\ndef greet(name: str) -> str:\n    """Return a greeting."""\n    return f"Hello, {name}!"\n',
            language="python",
        ),
        ContentChunk(
            id=ContentChunk.make_id(project_id, file_path, 1),
            project_id=project_id,
            file_path=file_path,
            chunk_index=1,
            line_start=91,
            line_end=150,
            content='class Calculator:\n    """A simple calculator."""\n    def add(self, a: int, b: int) -> int:\n        return a + b\n',
            language="python",
        ),
    ]


def test_upsert_content_chunks(code_storage: CodeIndexStorage) -> None:
    """Content chunks can be upserted."""
    chunks = _make_chunks()
    count = code_storage.upsert_content_chunks(chunks)
    assert count == 2


def test_upsert_empty_chunks(code_storage: CodeIndexStorage) -> None:
    """Upserting empty list returns 0."""
    assert code_storage.upsert_content_chunks([]) == 0


def test_delete_content_chunks_for_file(code_storage: CodeIndexStorage) -> None:
    """Deleting chunks for a file removes only that file's chunks."""
    chunks1 = _make_chunks(file_path="a.py")
    chunks2 = _make_chunks(file_path="b.py")
    code_storage.upsert_content_chunks(chunks1)
    code_storage.upsert_content_chunks(chunks2)

    code_storage.delete_content_chunks_for_file("proj-1", "a.py")

    # b.py chunks should still exist
    results = code_storage.search_content_fts("Calculator", "proj-1")
    file_paths = {r["file_path"] for r in results}
    assert "a.py" not in file_paths
    assert "b.py" in file_paths


def test_delete_content_chunks_for_project(code_storage: CodeIndexStorage) -> None:
    """Deleting chunks for a project removes all chunks."""
    code_storage.upsert_content_chunks(_make_chunks())
    code_storage.delete_content_chunks_for_project("proj-1")

    results = code_storage.search_content_fts("greet", "proj-1")
    assert results == []


def test_search_content_fts_finds_text(code_storage: CodeIndexStorage) -> None:
    """FTS search finds text in content chunks."""
    code_storage.upsert_content_chunks(_make_chunks())

    results = code_storage.search_content_fts("greeting", "proj-1")
    assert len(results) >= 1
    assert results[0]["file_path"] == "src/app.py"
    assert results[0]["language"] == "python"
    assert "line_start" in results[0]


def test_search_content_fts_filter_by_file(code_storage: CodeIndexStorage) -> None:
    """FTS search can be filtered to a specific file."""
    chunks1 = _make_chunks(file_path="a.py")
    chunks2 = _make_chunks(file_path="b.py")
    code_storage.upsert_content_chunks(chunks1)
    code_storage.upsert_content_chunks(chunks2)

    results = code_storage.search_content_fts("Calculator", "proj-1", file_path="a.py")
    assert all(r["file_path"] == "a.py" for r in results)


def test_search_content_fts_empty_query(code_storage: CodeIndexStorage) -> None:
    """Empty query returns no results."""
    code_storage.upsert_content_chunks(_make_chunks())
    assert code_storage.search_content_fts("", "proj-1") == []
    assert code_storage.search_content_fts("   ", "proj-1") == []


def test_search_content_fts_no_match(code_storage: CodeIndexStorage) -> None:
    """Query with no matching content returns empty list."""
    code_storage.upsert_content_chunks(_make_chunks())
    results = code_storage.search_content_fts("zzz_nonexistent_zzz", "proj-1")
    assert results == []


# ── Summary freshness ──────────────────────────────────────────────────


def test_upsert_nulls_summary_on_hash_change(
    code_storage: CodeIndexStorage, sample_symbols: list[Symbol]
) -> None:
    """When content_hash changes, summary is cleared for regeneration."""
    sym = sample_symbols[0]
    code_storage.upsert_symbols([sym])
    code_storage.update_symbol_summary(sym.id, "Greets a person by name.")

    # Verify summary is set
    retrieved = code_storage.get_symbol(sym.id)
    assert retrieved is not None
    assert retrieved.summary == "Greets a person by name."

    # Re-upsert with different content_hash
    sym.content_hash = "changed_hash"
    code_storage.upsert_symbols([sym])

    retrieved = code_storage.get_symbol(sym.id)
    assert retrieved is not None
    assert retrieved.summary is None, "Summary should be nulled when content_hash changes"


def test_upsert_preserves_summary_on_same_hash(
    code_storage: CodeIndexStorage, sample_symbols: list[Symbol]
) -> None:
    """When content_hash stays the same, summary is preserved."""
    sym = sample_symbols[0]
    code_storage.upsert_symbols([sym])
    code_storage.update_symbol_summary(sym.id, "Greets a person by name.")

    # Re-upsert with same content_hash
    code_storage.upsert_symbols([sym])

    retrieved = code_storage.get_symbol(sym.id)
    assert retrieved is not None
    assert retrieved.summary == "Greets a person by name."


def test_get_unsummarized_symbols(
    code_storage: CodeIndexStorage, sample_symbols: list[Symbol]
) -> None:
    """get_unsummarized_symbols returns only symbols without summaries."""
    code_storage.upsert_symbols(sample_symbols)

    # All three should be unsummarized
    unsummarized = code_storage.get_unsummarized_symbols("proj-1")
    assert len(unsummarized) == 3

    # Summarize one
    code_storage.update_symbol_summary(sample_symbols[0].id, "A greeting function.")

    unsummarized = code_storage.get_unsummarized_symbols("proj-1")
    assert len(unsummarized) == 2
    assert all(s.id != sample_symbols[0].id for s in unsummarized)


def test_get_unsummarized_symbols_filters_by_kind(
    code_storage: CodeIndexStorage, sample_symbols: list[Symbol]
) -> None:
    """get_unsummarized_symbols respects the kinds filter."""
    code_storage.upsert_symbols(sample_symbols)

    # Only functions
    unsummarized = code_storage.get_unsummarized_symbols("proj-1", kinds=["function"])
    assert len(unsummarized) == 1
    assert unsummarized[0].kind == "function"


def test_get_unsummarized_symbols_respects_limit(
    code_storage: CodeIndexStorage, sample_symbols: list[Symbol]
) -> None:
    """get_unsummarized_symbols respects the limit parameter."""
    code_storage.upsert_symbols(sample_symbols)

    unsummarized = code_storage.get_unsummarized_symbols("proj-1", limit=1)
    assert len(unsummarized) == 1


def test_update_symbol_summary(
    code_storage: CodeIndexStorage, sample_symbols: list[Symbol]
) -> None:
    """update_symbol_summary sets the summary field."""
    sym = sample_symbols[0]
    code_storage.upsert_symbols([sym])

    result = code_storage.update_symbol_summary(sym.id, "Returns a greeting string.")
    assert result is True

    retrieved = code_storage.get_symbol(sym.id)
    assert retrieved is not None
    assert retrieved.summary == "Returns a greeting string."


def test_update_symbol_summary_nonexistent(code_storage: CodeIndexStorage) -> None:
    """update_symbol_summary returns False for nonexistent symbol."""
    result = code_storage.update_symbol_summary("nonexistent-id", "Some summary.")
    assert result is False
