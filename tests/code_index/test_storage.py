"""Tests for code_index.storage CRUD operations."""

from __future__ import annotations

import pytest

from gobby.code_index.models import IndexedFile, IndexedProject, Symbol
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


def test_get_symbols_for_file(
    code_storage: CodeIndexStorage, sample_symbols: list[Symbol]
) -> None:
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
    results = code_storage.search_symbols_by_name(
        "Calc", "proj-1", kind="class"
    )
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


# ── Summaries ───────────────────────────────────────────────────────────


def test_update_symbol_summary(
    code_storage: CodeIndexStorage, sample_symbols: list[Symbol]
) -> None:
    """Set and retrieve a symbol summary."""
    sym = sample_symbols[0]
    code_storage.upsert_symbols([sym])

    code_storage.update_symbol_summary(sym.id, "Returns a greeting string.")
    retrieved = code_storage.get_symbol(sym.id)
    assert retrieved is not None
    assert retrieved.summary == "Returns a greeting string."


def test_get_symbols_without_summaries(
    code_storage: CodeIndexStorage, sample_symbols: list[Symbol]
) -> None:
    """Returns symbols that have no summary yet."""
    code_storage.upsert_symbols(sample_symbols)

    # All three should lack summaries
    unsummarized = code_storage.get_symbols_without_summaries("proj-1")
    assert len(unsummarized) == 3

    # Summarize one
    code_storage.update_symbol_summary(sample_symbols[0].id, "A greeting function.")
    unsummarized = code_storage.get_symbols_without_summaries("proj-1")
    assert len(unsummarized) == 2


def test_get_symbols_without_summaries_limit(
    code_storage: CodeIndexStorage, sample_symbols: list[Symbol]
) -> None:
    """Limit parameter caps results."""
    code_storage.upsert_symbols(sample_symbols)

    unsummarized = code_storage.get_symbols_without_summaries("proj-1", limit=1)
    assert len(unsummarized) == 1


# ── Counts ──────────────────────────────────────────────────────────────


def test_count_symbols(
    code_storage: CodeIndexStorage, sample_symbols: list[Symbol]
) -> None:
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
