"""Tests for code_index.searcher (hybrid search with graceful degradation)."""

from __future__ import annotations

import pytest

from gobby.code_index.models import Symbol
from gobby.code_index.searcher import CodeSearcher, RRF_K, _rrf_score
from gobby.code_index.storage import CodeIndexStorage

pytestmark = pytest.mark.unit


@pytest.fixture
def searcher(code_storage: CodeIndexStorage) -> CodeSearcher:
    """SQLite-only searcher (no Qdrant/Neo4j)."""
    return CodeSearcher(storage=code_storage)


# ── RRF scoring ─────────────────────────────────────────────────────────


def test_rrf_score_math() -> None:
    """RRF score = 1 / (K + rank)."""
    assert _rrf_score(0) == pytest.approx(1.0 / RRF_K)
    assert _rrf_score(1) == pytest.approx(1.0 / (RRF_K + 1))
    assert _rrf_score(10) == pytest.approx(1.0 / (RRF_K + 10))


def test_rrf_score_decreases_with_rank() -> None:
    """Higher rank -> lower score."""
    assert _rrf_score(0) > _rrf_score(1) > _rrf_score(10) > _rrf_score(100)


# ── SQLite-only search ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sqlite_search_returns_results(
    searcher: CodeSearcher,
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
) -> None:
    """Search returns matching symbols from SQLite."""
    code_storage.upsert_symbols(sample_symbols)

    results = await searcher.search("greet", "proj-1")
    assert len(results) >= 1
    assert any(r["name"] == "greet" for r in results)


@pytest.mark.asyncio
async def test_sqlite_search_includes_score(
    searcher: CodeSearcher,
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
) -> None:
    """Results include _score and _sources metadata."""
    code_storage.upsert_symbols(sample_symbols)

    results = await searcher.search("greet", "proj-1")
    assert len(results) >= 1
    first = results[0]
    assert "_score" in first
    assert "_sources" in first
    assert "name" in first["_sources"]
    assert first["_score"] > 0


@pytest.mark.asyncio
async def test_sqlite_search_respects_limit(
    searcher: CodeSearcher,
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
) -> None:
    """Limit parameter caps result count."""
    code_storage.upsert_symbols(sample_symbols)

    results = await searcher.search("a", "proj-1", limit=1)
    assert len(results) <= 1


@pytest.mark.asyncio
async def test_sqlite_search_with_kind_filter(
    searcher: CodeSearcher,
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
) -> None:
    """Kind filter narrows results."""
    code_storage.upsert_symbols(sample_symbols)

    results = await searcher.search("Calc", "proj-1", kind="class")
    assert all(r["kind"] == "class" for r in results)


@pytest.mark.asyncio
async def test_sqlite_search_no_results(
    searcher: CodeSearcher,
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
) -> None:
    """Search with no matches returns empty list."""
    code_storage.upsert_symbols(sample_symbols)

    results = await searcher.search("zzz_nonexistent_zzz", "proj-1")
    assert results == []


# ── Graceful degradation ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_graceful_degradation_no_qdrant(
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
) -> None:
    """Search works without Qdrant (vector_store=None)."""
    code_storage.upsert_symbols(sample_symbols)
    searcher = CodeSearcher(
        storage=code_storage,
        vector_store=None,
        embed_fn=None,
    )

    results = await searcher.search("greet", "proj-1")
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_graceful_degradation_no_neo4j(
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
) -> None:
    """Search works without Neo4j (graph=None)."""
    code_storage.upsert_symbols(sample_symbols)
    searcher = CodeSearcher(
        storage=code_storage,
        graph=None,
    )

    results = await searcher.search("Calculator", "proj-1")
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_graceful_degradation_qdrant_failure(
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
) -> None:
    """Search degrades gracefully when Qdrant raises an exception."""
    from unittest.mock import AsyncMock, MagicMock

    code_storage.upsert_symbols(sample_symbols)

    # Mock a vector store that always fails
    failing_vector = MagicMock()
    failing_embed = AsyncMock(side_effect=RuntimeError("connection refused"))

    searcher = CodeSearcher(
        storage=code_storage,
        vector_store=failing_vector,
        embed_fn=failing_embed,
    )

    # Should still return SQLite results
    results = await searcher.search("greet", "proj-1")
    assert len(results) >= 1


# ── search_text (sync) ──────────────────────────────────────────────────


def test_search_text_sync(
    searcher: CodeSearcher,
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
) -> None:
    """Synchronous search_text returns symbol dicts."""
    code_storage.upsert_symbols(sample_symbols)

    results = searcher.search_text("add", "proj-1")
    assert len(results) >= 1
    assert any(r["name"] == "add" for r in results)


def test_search_text_with_file_filter(
    searcher: CodeSearcher,
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
) -> None:
    """search_text with file_path filter."""
    code_storage.upsert_symbols(sample_symbols)

    results = searcher.search_text(
        "greet", "proj-1", file_path="src/app.py"
    )
    assert len(results) >= 1
    assert all(r["file_path"] == "src/app.py" for r in results)
