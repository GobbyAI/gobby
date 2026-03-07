"""Tests for code_index.indexer orchestration."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.code_index.indexer import CodeIndexer
from gobby.code_index.models import IndexedFile
from gobby.code_index.parser import CodeParser
from gobby.code_index.storage import CodeIndexStorage
from gobby.config.code_index import CodeIndexConfig

pytestmark = pytest.mark.unit


@pytest.fixture
def config() -> CodeIndexConfig:
    return CodeIndexConfig()


@pytest.fixture
def parser(config: CodeIndexConfig) -> CodeParser:
    return CodeParser(config)


@pytest.fixture
def indexer(
    code_storage: CodeIndexStorage, parser: CodeParser, config: CodeIndexConfig
) -> CodeIndexer:
    """CodeIndexer with no vector/graph/summarizer backends."""
    return CodeIndexer(
        storage=code_storage,
        parser=parser,
        config=config,
    )


@pytest.fixture
def sample_project(tmp_path: Path, sample_python_source: str) -> Path:
    """Create a small project directory with Python files."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text(sample_python_source)
    (src / "utils.py").write_text("def helper() -> str:\n    return 'ok'\n")
    return tmp_path


# ── index_directory ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_index_directory_basic(
    indexer: CodeIndexer, sample_project: Path
) -> None:
    """index_directory indexes files and returns result."""
    result = await indexer.index_directory(
        root_path=str(sample_project),
        project_id="proj-1",
        incremental=False,
    )

    assert result.files_indexed >= 2
    assert result.symbols_found > 0
    assert result.duration_ms >= 0
    assert result.errors == []


@pytest.mark.asyncio
async def test_index_directory_incremental_skips_unchanged(
    indexer: CodeIndexer, sample_project: Path
) -> None:
    """Second incremental run skips unchanged files."""
    # First full index
    r1 = await indexer.index_directory(
        str(sample_project), "proj-1", incremental=False
    )
    assert r1.files_indexed >= 2

    # Second incremental index - files haven't changed
    r2 = await indexer.index_directory(
        str(sample_project), "proj-1", incremental=True
    )
    assert r2.files_skipped >= 2
    assert r2.files_indexed == 0


@pytest.mark.asyncio
async def test_index_directory_incremental_reindexes_changed(
    indexer: CodeIndexer, sample_project: Path
) -> None:
    """Incremental run reindexes files that changed."""
    # First index
    await indexer.index_directory(
        str(sample_project), "proj-1", incremental=False
    )

    # Modify one file
    (sample_project / "src" / "utils.py").write_text(
        "def helper() -> str:\n    return 'updated'\n\ndef new_func(): pass\n"
    )

    # Incremental should catch the change
    r2 = await indexer.index_directory(
        str(sample_project), "proj-1", incremental=True
    )
    assert r2.files_indexed >= 1


@pytest.mark.asyncio
async def test_index_directory_not_a_dir(indexer: CodeIndexer) -> None:
    """Non-existent directory returns error."""
    result = await indexer.index_directory(
        "/nonexistent/path", "proj-1"
    )
    assert len(result.errors) > 0
    assert "Not a directory" in result.errors[0]


@pytest.mark.asyncio
async def test_index_directory_updates_project_stats(
    indexer: CodeIndexer, sample_project: Path
) -> None:
    """index_directory updates project stats in storage."""
    await indexer.index_directory(
        str(sample_project), "proj-1", incremental=False
    )

    stats = indexer.storage.get_project_stats("proj-1")
    assert stats is not None
    assert stats.total_files >= 2
    assert stats.total_symbols > 0
    assert stats.index_duration_ms >= 0


# ── index_file ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_index_file_stores_symbols(
    indexer: CodeIndexer, sample_project: Path
) -> None:
    """index_file stores parsed symbols in storage."""
    file_path = str(sample_project / "src" / "app.py")
    symbols = await indexer.index_file(
        file_path, "proj-1", str(sample_project)
    )

    assert symbols is not None
    assert len(symbols) > 0

    # Verify stored in DB
    stored = indexer.storage.get_symbols_for_file("proj-1", "src/app.py")
    assert len(stored) == len(symbols)


@pytest.mark.asyncio
async def test_index_file_stores_file_record(
    indexer: CodeIndexer, sample_project: Path
) -> None:
    """index_file creates a file record in storage."""
    file_path = str(sample_project / "src" / "app.py")
    await indexer.index_file(file_path, "proj-1", str(sample_project))

    f = indexer.storage.get_file("proj-1", "src/app.py")
    assert f is not None
    assert f.language == "python"
    assert f.content_hash != ""
    assert f.byte_size > 0


@pytest.mark.asyncio
async def test_index_file_returns_none_for_skip(
    indexer: CodeIndexer, sample_project: Path
) -> None:
    """index_file returns None for files that should be skipped."""
    binary = sample_project / "image.bin"
    binary.write_bytes(b"\x89PNG\x00\x00data")

    result = await indexer.index_file(
        str(binary), "proj-1", str(sample_project)
    )
    assert result is None


# ── index_changed_files ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_index_changed_files_handles_deleted(
    indexer: CodeIndexer, sample_project: Path
) -> None:
    """index_changed_files cleans up deleted files."""
    # First, index a file
    file_path = str(sample_project / "src" / "app.py")
    await indexer.index_file(file_path, "proj-1", str(sample_project))

    # Verify it's stored
    assert indexer.storage.get_symbols_for_file("proj-1", "src/app.py") != []

    # Delete the file
    (sample_project / "src" / "app.py").unlink()

    # Process the "changed" file (which is now deleted)
    result = await indexer.index_changed_files(
        "proj-1",
        str(sample_project),
        ["src/app.py"],
    )

    # Symbols should be cleaned up
    assert indexer.storage.get_symbols_for_file("proj-1", "src/app.py") == []


@pytest.mark.asyncio
async def test_index_changed_files_result(
    indexer: CodeIndexer, sample_project: Path
) -> None:
    """index_changed_files returns correct counts."""
    result = await indexer.index_changed_files(
        "proj-1",
        str(sample_project),
        ["src/app.py", "src/utils.py"],
    )
    assert result.files_indexed == 2
    assert result.symbols_found > 0
    assert result.duration_ms >= 0


# ── invalidate ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalidate_clears_all_data(
    indexer: CodeIndexer, sample_project: Path
) -> None:
    """invalidate removes all symbols and files for a project."""
    # Index first
    await indexer.index_directory(
        str(sample_project), "proj-1", incremental=False
    )

    # Verify data exists
    assert indexer.storage.count_symbols("proj-1") > 0
    assert indexer.storage.count_files("proj-1") > 0

    # Invalidate
    await indexer.invalidate("proj-1")

    # Verify all gone
    assert indexer.storage.count_symbols("proj-1") == 0
    assert indexer.storage.count_files("proj-1") == 0
