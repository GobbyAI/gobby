"""Tests for VectorStore (Qdrant-based vector storage)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from gobby.memory import vectorstore as vectorstore_module
from gobby.memory.vectorstore import (
    VectorStore,
    VectorStoreUnavailableError,
    is_recoverable_vector_store_error,
)

# Deterministic UUIDs for test reproducibility
MEM_1 = str(uuid.uuid5(uuid.NAMESPACE_DNS, "mem-1"))
MEM_2 = str(uuid.uuid5(uuid.NAMESPACE_DNS, "mem-2"))
MEM_3 = str(uuid.uuid5(uuid.NAMESPACE_DNS, "mem-3"))
MEM_A = str(uuid.uuid5(uuid.NAMESPACE_DNS, "mem-A"))
MEM_B = str(uuid.uuid5(uuid.NAMESPACE_DNS, "mem-B"))


def test_qdrant_client_importable() -> None:
    """qdrant-client package should be importable after dependency addition."""
    import qdrant_client  # noqa: F401

    assert hasattr(qdrant_client, "QdrantClient")


@pytest.fixture
async def vector_store(tmp_path) -> AsyncGenerator[VectorStore]:
    """Create a VectorStore using Qdrant embedded mode with a temp directory."""
    store = VectorStore(
        path=str(tmp_path / "qdrant"),
        collection_name="test_memories",
        embedding_dim=4,  # Small dim for fast tests
    )
    await store.initialize()
    yield store
    await store.close()


def _make_embedding(seed: float = 1.0, dim: int = 4) -> list[float]:
    """Create a deterministic embedding vector."""
    return [seed * (i + 1) / dim for i in range(dim)]


@pytest.mark.asyncio
async def test_initialize_creates_collection(tmp_path) -> None:
    """initialize() should create a Qdrant collection with cosine distance."""
    store = VectorStore(
        path=str(tmp_path / "qdrant"),
        collection_name="init_test",
        embedding_dim=4,
    )
    await store.initialize()

    count = await store.count()
    assert count == 0

    await store.close()


@pytest.mark.asyncio
async def test_initialize_idempotent(tmp_path) -> None:
    """Calling initialize() twice should not fail or reset data."""
    store = VectorStore(
        path=str(tmp_path / "qdrant"),
        collection_name="idem_test",
        embedding_dim=4,
    )
    await store.initialize()
    await store.upsert(MEM_1, _make_embedding(1.0), {"content": "hello"})
    assert await store.count() == 1

    # Re-initialize should not lose data
    await store.initialize()
    assert await store.count() == 1

    await store.close()


@pytest.mark.asyncio
async def test_operation_lazily_initializes(tmp_path) -> None:
    """Async operations should initialize Qdrant on first use."""
    store = VectorStore(
        path=str(tmp_path / "qdrant"),
        collection_name="lazy_test",
        embedding_dim=4,
    )

    await store.upsert(MEM_1, _make_embedding(1.0), {"content": "lazy"})

    assert await store.count() == 1
    await store.close()


@pytest.mark.asyncio
async def test_lazy_init_backoff_suppresses_repeated_attempts(monkeypatch) -> None:
    """Failed lazy init should not hammer Qdrant before the backoff expires."""
    now = 1000.0
    monkeypatch.setattr(vectorstore_module.time, "monotonic", lambda: now)

    client = MagicMock()
    client.collection_exists.side_effect = ResponseHandlingException(Exception("down"))

    with patch("gobby.memory.vectorstore.QdrantClient", return_value=client) as qdrant_cls:
        store = VectorStore(url="http://qdrant:6333", collection_name="retry_test")

        with pytest.raises(VectorStoreUnavailableError, match="VectorStore not initialized"):
            await store.count()

        with pytest.raises(VectorStoreUnavailableError, match="VectorStore not initialized"):
            await store.count()

    assert qdrant_cls.call_count == 1
    assert client.collection_exists.call_count == 1


@pytest.mark.asyncio
async def test_lazy_init_retries_after_backoff(monkeypatch) -> None:
    """Lazy init should retry after elapsed backoff and reset retry state on success."""
    now = 1000.0

    def monotonic() -> float:
        return now

    monkeypatch.setattr(vectorstore_module.time, "monotonic", monotonic)

    client = MagicMock()
    client.collection_exists.side_effect = [
        ResponseHandlingException(Exception("down")),
        False,
    ]
    client.count.return_value = SimpleNamespace(count=7)

    with patch("gobby.memory.vectorstore.QdrantClient", return_value=client):
        store = VectorStore(url="http://qdrant:6333", collection_name="retry_test")

        with pytest.raises(VectorStoreUnavailableError):
            await store.count()

        now += 5.1
        assert await store.count() == 7

    assert store._retry_backoff_seconds == 5.0
    assert store._next_retry_at == 0.0
    client.create_collection.assert_called_once()


@pytest.mark.asyncio
async def test_transient_operation_error_resets_client(monkeypatch) -> None:
    """Recoverable operation failures should drop the client for lazy re-init."""
    now = 1000.0
    monkeypatch.setattr(vectorstore_module.time, "monotonic", lambda: now)

    store = VectorStore(collection_name="operation_test", embedding_dim=4)
    client = MagicMock()
    client.upsert.side_effect = httpx.ConnectError("qdrant down")
    store._client = client

    with pytest.raises(VectorStoreUnavailableError, match="VectorStore not initialized"):
        await store.upsert(MEM_1, _make_embedding(1.0), {"content": "boom"})

    assert store._client is None
    assert store._next_retry_at == 1005.0


def test_count_sync_returns_zero_when_uninitialized() -> None:
    store = VectorStore(collection_name="sync_test")

    assert store.count_sync() == 0


@pytest.mark.parametrize(
    "error",
    [
        ResponseHandlingException(Exception("down")),
        UnexpectedResponse(503, "Service Unavailable", b"down", httpx.Headers()),
        httpx.ConnectTimeout("timeout"),
    ],
)
def test_recoverable_vector_store_errors(error: BaseException) -> None:
    assert is_recoverable_vector_store_error(error) is True


@pytest.mark.asyncio
async def test_upsert_and_count(vector_store: VectorStore) -> None:
    """upsert() should insert a point; count() should reflect it."""
    await vector_store.upsert(MEM_1, _make_embedding(1.0), {"content": "hello"})
    assert await vector_store.count() == 1

    await vector_store.upsert(MEM_2, _make_embedding(2.0), {"content": "world"})
    assert await vector_store.count() == 2


@pytest.mark.asyncio
async def test_upsert_overwrites(vector_store: VectorStore) -> None:
    """upsert() with same ID should update, not duplicate."""
    await vector_store.upsert(MEM_1, _make_embedding(1.0), {"content": "v1"})
    await vector_store.upsert(MEM_1, _make_embedding(2.0), {"content": "v2"})
    assert await vector_store.count() == 1


@pytest.mark.asyncio
async def test_search_returns_results(vector_store: VectorStore) -> None:
    """search() should return (memory_id, score) pairs sorted by relevance."""
    await vector_store.upsert(MEM_1, _make_embedding(1.0), {"content": "cat"})
    await vector_store.upsert(MEM_2, _make_embedding(1.1), {"content": "kitten"})
    await vector_store.upsert(MEM_3, _make_embedding(5.0), {"content": "airplane"})

    results = await vector_store.search(_make_embedding(1.0), limit=2)

    assert len(results) == 2
    # Each result is (memory_id, score)
    assert results[0][0] == MEM_1  # Exact match should be first
    assert isinstance(results[0][1], float)
    assert results[0][1] >= results[1][1]  # Sorted by score desc


@pytest.mark.asyncio
async def test_search_with_project_id_filter(vector_store: VectorStore) -> None:
    """search() should filter by project_id when provided."""
    await vector_store.upsert(
        MEM_1, _make_embedding(1.0), {"content": "alpha", "project_id": "proj-A"}
    )
    await vector_store.upsert(
        MEM_2, _make_embedding(1.1), {"content": "beta", "project_id": "proj-B"}
    )

    # Filter to proj-A only
    results = await vector_store.search(
        _make_embedding(1.0), limit=10, filters={"project_id": "proj-A"}
    )
    assert len(results) == 1
    assert results[0][0] == MEM_1

    # Filter to proj-B only
    results = await vector_store.search(
        _make_embedding(1.0), limit=10, filters={"project_id": "proj-B"}
    )
    assert len(results) == 1
    assert results[0][0] == MEM_2


@pytest.mark.asyncio
async def test_search_empty_collection(vector_store: VectorStore) -> None:
    """search() on empty collection should return empty list."""
    results = await vector_store.search(_make_embedding(1.0), limit=5)
    assert results == []


@pytest.mark.asyncio
async def test_delete(vector_store: VectorStore) -> None:
    """delete() should remove a point."""
    await vector_store.upsert(MEM_1, _make_embedding(1.0), {"content": "hello"})
    assert await vector_store.count() == 1

    await vector_store.delete(MEM_1)
    assert await vector_store.count() == 0


@pytest.mark.asyncio
async def test_delete_nonexistent(vector_store: VectorStore) -> None:
    """delete() on nonexistent ID should not raise."""
    await vector_store.delete(MEM_1)  # Should not raise


@pytest.mark.asyncio
async def test_batch_upsert(vector_store: VectorStore) -> None:
    """batch_upsert() should insert multiple points at once."""
    items = [
        (MEM_1, _make_embedding(1.0), {"content": "one"}),
        (MEM_2, _make_embedding(2.0), {"content": "two"}),
        (MEM_3, _make_embedding(3.0), {"content": "three"}),
    ]
    await vector_store.batch_upsert(items)
    assert await vector_store.count() == 3


@pytest.mark.asyncio
async def test_batch_upsert_empty(vector_store: VectorStore) -> None:
    """batch_upsert() with empty list should not fail."""
    await vector_store.batch_upsert([])
    assert await vector_store.count() == 0


@pytest.mark.asyncio
async def test_rebuild(vector_store: VectorStore) -> None:
    """rebuild() should re-embed all memories from content list."""
    # Pre-populate
    await vector_store.upsert(MEM_1, _make_embedding(1.0), {"content": "old"})

    # Define memories to rebuild with
    memories = [
        {"id": MEM_A, "content": "alpha", "project_id": "proj-1"},
        {"id": MEM_B, "content": "beta", "project_id": "proj-2"},
    ]

    call_count = 0

    async def mock_embed_fn(text: str) -> list[float]:
        nonlocal call_count
        call_count += 1
        return _make_embedding(call_count)

    await vector_store.rebuild(memories, mock_embed_fn)

    # Old data should be gone, new data present
    assert await vector_store.count() == 2
    assert call_count == 2


@pytest.mark.asyncio
async def test_dimension_mismatch_logs_error(tmp_path, caplog) -> None:
    """initialize() should log error when collection dim mismatches configured dim."""
    import logging

    # Create a collection with dim=4
    store = VectorStore(
        path=str(tmp_path / "qdrant"),
        collection_name="dim_test",
        embedding_dim=4,
    )
    await store.initialize()
    await store.upsert(MEM_1, _make_embedding(1.0, dim=4), {"content": "test"})
    await store.close()

    # Reopen with different dim — should log error
    store2 = VectorStore(
        path=str(tmp_path / "qdrant"),
        collection_name="dim_test",
        embedding_dim=768,
    )
    with caplog.at_level(logging.ERROR, logger="gobby.memory.vectorstore"):
        await store2.initialize()

    assert "dimension mismatch" in caplog.text.lower()
    assert "configured=768" in caplog.text
    assert "existing=4" in caplog.text
    await store2.close()


@pytest.mark.asyncio
async def test_dimension_match_no_error(tmp_path, caplog) -> None:
    """initialize() should NOT log error when dimensions match."""
    import logging

    store = VectorStore(
        path=str(tmp_path / "qdrant"),
        collection_name="match_test",
        embedding_dim=4,
    )
    await store.initialize()
    await store.close()

    store2 = VectorStore(
        path=str(tmp_path / "qdrant"),
        collection_name="match_test",
        embedding_dim=4,
    )
    with caplog.at_level(logging.ERROR, logger="gobby.memory.vectorstore"):
        await store2.initialize()

    assert "dimension mismatch" not in caplog.text.lower()
    await store2.close()


@pytest.mark.asyncio
async def test_close(tmp_path) -> None:
    """close() should work without error."""
    store = VectorStore(
        path=str(tmp_path / "qdrant"),
        collection_name="close_test",
        embedding_dim=4,
    )
    await store.initialize()
    await store.close()
    # Calling close again should not raise
    await store.close()


@pytest.mark.asyncio
async def test_get_collection_dimension_returns_none_on_client_error(caplog) -> None:
    store = VectorStore(collection_name="test_memories", embedding_dim=4)
    client = MagicMock()
    client.get_collection.side_effect = RuntimeError("boom")
    store._client = client

    dimension = await store.get_collection_dimension()

    assert dimension is None
    assert "Failed to read Qdrant collection dimension" in caplog.text
