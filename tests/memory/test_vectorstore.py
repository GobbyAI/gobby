"""Tests for VectorStore (Qdrant-based vector storage)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from qdrant_client.models import Distance, VectorParams

from gobby.config.persistence import MemoryConfig
from gobby.memory import vectorstore as vectorstore_module
from gobby.memory.services._search_paths import _qdrant_hits_or_empty
from gobby.memory.services.crossref import CrossrefService
from gobby.memory.vectorstore import (
    VectorStore,
    VectorStoreCollectionDimensionError,
    VectorStoreUnavailableError,
    is_recoverable_vector_store_error,
    memory_scope_filter,
)
from gobby.storage.embedding_generation_state import EmbeddingGenerationLeaseLost
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.memories_scope import MemoryScope

pytestmark = pytest.mark.unit

# Deterministic UUIDs for test reproducibility
MEM_1 = str(uuid.uuid5(uuid.NAMESPACE_DNS, "mem-1"))
MEM_2 = str(uuid.uuid5(uuid.NAMESPACE_DNS, "mem-2"))
MEM_3 = str(uuid.uuid5(uuid.NAMESPACE_DNS, "mem-3"))
MEM_A = str(uuid.uuid5(uuid.NAMESPACE_DNS, "mem-A"))
MEM_B = str(uuid.uuid5(uuid.NAMESPACE_DNS, "mem-B"))


def test_qdrant_client_importable() -> None:
    """qdrant-client package should be importable after dependency addition."""
    import qdrant_client

    assert hasattr(qdrant_client, "QdrantClient")


@pytest.fixture
async def vector_store(tmp_path: Path) -> AsyncGenerator[VectorStore]:
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


def _collection_info(dim: int) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(
                vectors=VectorParams(size=dim, distance=Distance.COSINE),
            ),
        ),
    )


class _TrackingAsyncLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.enter_count = 0
        self.active_count = 0
        self.max_active_count = 0

    async def __aenter__(self) -> None:
        await self._lock.acquire()
        self.enter_count += 1
        self.active_count += 1
        self.max_active_count = max(self.max_active_count, self.active_count)

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        self.active_count -= 1
        self._lock.release()


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


@pytest.mark.unit
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
    client.collection_exists = AsyncMock(side_effect=ResponseHandlingException(Exception("down")))
    client.close = AsyncMock()

    with patch("gobby.memory.vectorstore.AsyncQdrantClient", return_value=client) as qdrant_cls:
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
    client.collection_exists = AsyncMock(
        side_effect=[ResponseHandlingException(Exception("down")), False]
    )
    client.create_collection = AsyncMock()
    client.count = AsyncMock(return_value=SimpleNamespace(count=7))
    client.close = AsyncMock()

    with patch("gobby.memory.vectorstore.AsyncQdrantClient", return_value=client):
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


def test_count_sync_surface_removed() -> None:
    store = VectorStore(collection_name="sync_test")
    assert not hasattr(store, "count_sync")


@pytest.mark.parametrize(
    "error",
    [
        ResponseHandlingException(Exception("down")),
        UnexpectedResponse(503, "Service Unavailable", b"down", httpx.Headers()),
        httpx.ConnectTimeout("timeout"),
        EmbeddingGenerationLeaseLost("Embedding generation serving is fenced"),
    ],
)
def test_recoverable_vector_store_errors(error: BaseException) -> None:
    assert is_recoverable_vector_store_error(error) is True


def test_timeout_error_is_not_recoverable_vector_store_error() -> None:
    assert is_recoverable_vector_store_error(TimeoutError("deadline")) is False


def test_fenced_lease_search_uses_availability_fallback_without_qdrant_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = EmbeddingGenerationLeaseLost("Embedding generation serving is fenced")
    service = MagicMock()

    with caplog.at_level(logging.WARNING):
        result = _qdrant_hits_or_empty(
            error,
            service=service,
            caller="test",
            project_id="project-a",
            candidate_limit=5,
            path="vector",
            recoverable_message="Vector search unavailable",
        )

    assert result == []
    service._log_vector_store_failure.assert_called_once_with("Vector search unavailable", error)
    assert "Qdrant search failed" not in caplog.text


@pytest.mark.asyncio
async def test_list_collection_names_uses_public_initialized_accessor(
    vector_store: VectorStore,
) -> None:
    names = await vector_store.list_collection_names()

    assert "test_memories" in names


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
async def test_scroll_ids_with_project_filter(vector_store: VectorStore) -> None:
    await vector_store.upsert(MEM_1, _make_embedding(1.0), {"project_id": "proj-A"})
    await vector_store.upsert(MEM_2, _make_embedding(2.0), {"project_id": "proj-B"})

    result = await vector_store.scroll_ids(filters={"project_id": "proj-A"})

    assert result == [MEM_1]


def test_memory_scope_filter_includes_project_and_explicit_global_payloads() -> None:
    scope_filter = memory_scope_filter(MemoryScope.project_visible("proj-A"))

    assert scope_filter is not None
    dumped = scope_filter.model_dump(mode="python")
    assert dumped["should"] == [
        {
            "key": "project_id",
            "match": {"value": "proj-A"},
            "range": None,
            "geo_bounding_box": None,
            "geo_radius": None,
            "geo_polygon": None,
            "values_count": None,
            "is_empty": None,
            "is_null": None,
        },
        {
            "key": "is_global",
            "match": {"value": True},
            "range": None,
            "geo_bounding_box": None,
            "geo_radius": None,
            "geo_polygon": None,
            "values_count": None,
            "is_empty": None,
            "is_null": None,
        },
    ]


def test_memory_scope_filter_requires_canonical_memory_type() -> None:
    scope_filter = memory_scope_filter(MemoryScope.project_visible("proj-A"), "pattern")

    assert scope_filter is not None
    dumped = scope_filter.model_dump(mode="python")
    assert dumped["must"][0]["key"] == "memory_type"
    assert dumped["must"][0]["match"] == {"value": "pattern"}
    with pytest.raises(ValueError, match="Invalid memory_type 'debugging_pattern'"):
        memory_scope_filter(MemoryScope.project_visible("proj-A"), "debugging_pattern")


@pytest.mark.asyncio
async def test_search_with_memory_scope_filter_includes_explicit_globals(
    vector_store: VectorStore,
) -> None:
    """Project-visible Qdrant scope includes explicit globals and excludes other projects.

    Every payload carries concrete ownership and an explicit visibility bit.
    """
    await vector_store.upsert(
        MEM_1,
        _make_embedding(1.0),
        {"content": "universal fact", "project_id": "owner-A", "is_global": True},
    )
    await vector_store.upsert(
        MEM_2,
        _make_embedding(1.1),
        {"content": "proj-A specific", "project_id": "proj-A", "is_global": False},
    )
    await vector_store.upsert(
        MEM_3,
        _make_embedding(1.2),
        {"content": "proj-B specific", "project_id": "proj-B", "is_global": False},
    )

    scope_filter = memory_scope_filter(MemoryScope.project_visible("proj-A"))
    assert scope_filter is not None

    results = await vector_store.search(_make_embedding(1.0), limit=10, filters=scope_filter)
    result_ids = [rid for rid, _score in results]

    assert MEM_1 in result_ids, "explicit global must be returned"
    assert MEM_2 in result_ids, "owner-project memory must be returned"
    assert MEM_3 not in result_ids, "other project's memory must be excluded (no leak)"


@pytest.mark.asyncio
async def test_crossref_create_fills_links_from_project_and_global_only(
    vector_store: VectorStore,
    temp_db,
) -> None:
    db = temp_db
    project_a = "11111111-1111-4111-8111-111111111111"
    project_b = "22222222-2222-4222-8222-222222222222"
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (project_a, "Project A"))
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (project_b, "Project B"))
    storage = LocalMemoryManager(db)
    source = storage.create_memory(content="source", project_id=project_a)
    same_project = storage.create_memory(content="same project", project_id=project_a)
    global_memory = storage.create_memory(content="global", project_id=project_a, is_global=True)
    foreign = storage.create_memory(content="foreign", project_id=project_b)
    query = _make_embedding(1.0)

    await vector_store.upsert(source.id, query, {"project_id": project_a, "is_global": False})
    await vector_store.upsert(foreign.id, query, {"project_id": project_b, "is_global": False})
    await vector_store.upsert(
        same_project.id,
        _make_embedding(1.01),
        {"project_id": project_a, "is_global": False},
    )
    await vector_store.upsert(
        global_memory.id,
        _make_embedding(1.02),
        {"project_id": project_a, "is_global": True},
    )

    service = CrossrefService(
        storage=storage,
        vector_store=vector_store,
        embed_fn=AsyncMock(return_value=query),
        config=MemoryConfig(crossref_threshold=0.1, crossref_max_links=2),
    )
    created = await service.create(source)

    assert created == 2
    refs = storage.get_crossrefs(source.id, limit=10)
    targets = {ref.target_id if ref.source_id == source.id else ref.source_id for ref in refs}
    expected_similarity = {
        ref.target_id if ref.source_id == source.id else ref.source_id: ref.similarity
        for ref in refs
    }
    assert targets == {same_project.id, global_memory.id}
    assert foreign.id not in targets

    # A legacy bad edge is still hidden by the existing scoped read path.
    storage.create_crossref(source.id, foreign.id, 0.99)
    related = service.get_related(source.id, limit=10, project_id=project_a)
    assert {memory.id for memory in related} == {same_project.id, global_memory.id}
    assert {memory.id: memory.similarity for memory in related} == expected_similarity


@pytest.mark.asyncio
async def test_crossref_create_for_global_source_links_global_candidates_only(
    vector_store: VectorStore,
    temp_db,
) -> None:
    db = temp_db
    project_a = "11111111-1111-4111-8111-111111111111"
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (project_a, "Project A"))
    storage = LocalMemoryManager(db)
    source = storage.create_memory(content="global source", project_id=project_a, is_global=True)
    global_target = storage.create_memory(
        content="global target", project_id=project_a, is_global=True
    )
    project_target = storage.create_memory(content="project target", project_id=project_a)
    query = _make_embedding(2.0)

    await vector_store.upsert(source.id, query, {"project_id": project_a, "is_global": True})
    await vector_store.upsert(
        project_target.id,
        query,
        {"project_id": project_a, "is_global": False},
    )
    await vector_store.upsert(
        global_target.id,
        _make_embedding(2.01),
        {"project_id": project_a, "is_global": True},
    )

    service = CrossrefService(
        storage=storage,
        vector_store=vector_store,
        embed_fn=AsyncMock(return_value=query),
        config=MemoryConfig(crossref_threshold=0.1, crossref_max_links=1),
    )
    created = await service.create(source)

    assert created == 1
    refs = storage.get_crossrefs(source.id, limit=10)
    targets = {ref.target_id if ref.source_id == source.id else ref.source_id for ref in refs}
    assert targets == {global_target.id}
    assert project_target.id not in targets


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
    result = await vector_store.delete(MEM_1)

    assert result is None
    assert await vector_store.count() == 0


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
async def test_rebuild_same_dimension_does_not_recreate_collection(
    vector_store: VectorStore,
) -> None:
    await vector_store.upsert(MEM_1, _make_embedding(1.0), {"content": "old"})
    client = vector_store._client
    assert client is not None

    async def embed_fn(_text: str) -> list[float]:
        return _make_embedding()

    with (
        patch.object(client, "delete_collection", wraps=client.delete_collection) as delete_spy,
        patch.object(client, "create_collection", wraps=client.create_collection) as create_spy,
    ):
        await vector_store.rebuild(
            [{"id": MEM_A, "content": "alpha"}],
            embed_fn,
            recreate_on_mismatch=True,
        )

    assert set(await vector_store.scroll_ids()) == {MEM_A}
    assert await vector_store.count() == 1
    delete_spy.assert_not_called()
    create_spy.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_rebuild_calls_are_serialized() -> None:
    store = VectorStore(collection_name="mock_memories", embedding_dim=4)
    tracking_lock = _TrackingAsyncLock()
    store._rebuild_lock = tracking_lock
    client = MagicMock()
    client.collection_exists.return_value = True
    client.get_collection.return_value = _collection_info(4)
    client.scroll.return_value = ([], None)
    store._client = client

    first_embed_started = asyncio.Event()
    release_first_embed = asyncio.Event()
    embed_call_count = 0

    async def embed_fn(_text: str) -> list[float]:
        nonlocal embed_call_count
        embed_call_count += 1
        if embed_call_count == 1:
            first_embed_started.set()
            await release_first_embed.wait()
        return _make_embedding()

    first_rebuild = asyncio.create_task(
        store.rebuild([{"id": MEM_A, "content": "alpha"}], embed_fn)
    )
    await first_embed_started.wait()
    second_rebuild = asyncio.create_task(
        store.rebuild([{"id": MEM_B, "content": "beta"}], embed_fn)
    )
    release_first_embed.set()
    await asyncio.gather(first_rebuild, second_rebuild)

    assert tracking_lock.enter_count == 2
    assert tracking_lock.max_active_count == 1
    assert client.upsert.call_count == 2


@pytest.mark.asyncio
async def test_create_collection_conflict_is_idempotent_for_expected_dimension() -> None:
    store = VectorStore(collection_name="mock_memories", embedding_dim=4)
    client = MagicMock()
    client.collection_exists.return_value = False
    client.create_collection.side_effect = UnexpectedResponse(
        409,
        "Conflict",
        b"exists",
        httpx.Headers(),
    )
    client.get_collection.return_value = _collection_info(4)
    store._client = client

    await store.ensure_collection("mock_memories", embedding_dim=4)

    assert store._client is client
    assert store._next_retry_at == 0.0
    client.get_collection.assert_called_once_with("mock_memories")


@pytest.mark.asyncio
async def test_create_collection_conflict_rejects_unexpected_dimension() -> None:
    store = VectorStore(collection_name="mock_memories", embedding_dim=4)
    client = MagicMock()
    client.collection_exists.return_value = False
    client.create_collection.side_effect = UnexpectedResponse(
        409,
        "Conflict",
        b"exists",
        httpx.Headers(),
    )
    client.get_collection.return_value = _collection_info(5)
    store._client = client

    with pytest.raises(UnexpectedResponse) as exc_info:
        await store.ensure_collection("mock_memories", embedding_dim=4)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_ensure_collection_dimension_mismatch_fails_without_recreate() -> None:
    store = VectorStore(collection_name="mock_memories", embedding_dim=4)
    client = MagicMock()
    client.collection_exists.return_value = True
    client.get_collection.return_value = _collection_info(3)
    store._client = client

    with pytest.raises(VectorStoreCollectionDimensionError) as exc_info:
        await store.ensure_collection("mock_memories", embedding_dim=4)

    message = str(exc_info.value)
    assert "expected_dim=4, observed_dim=3" in message
    assert "Rebuild or migrate the collection" in message
    assert "Local/default nomic embeddings are usually 768 dimensions" in message
    assert "text-embedding-3-small" in message
    assert "embedding_dim=1536" in message
    client.delete_collection.assert_not_called()
    client.create_collection.assert_not_called()


@pytest.mark.asyncio
async def test_initialize_dimension_mismatch_preserves_active_collection() -> None:
    store = VectorStore(collection_name="mock_memories", embedding_dim=4)
    client = MagicMock()
    client.collection_exists.return_value = True
    client.get_collection.return_value = _collection_info(3)
    store._client = client

    await store.initialize()

    client.delete_collection.assert_not_called()
    client.create_collection.assert_not_called()
    assert store.status_snapshot() == {
        "state": "dimension_mismatch_pending_rebuild",
        "collection": "mock_memories",
        "configured_dimension": 4,
        "rebuild_required": True,
        "dimension_recovery": {
            "action": "temp_rebuild_required",
            "previous_dimension": 3,
            "configured_dimension": 4,
        },
    }


@pytest.mark.asyncio
async def test_rebuild_same_dimension_removes_stale_point_ids(
    vector_store: VectorStore,
) -> None:
    await vector_store.upsert(MEM_1, _make_embedding(1.0), {"content": "stale"})
    await vector_store.upsert(MEM_2, _make_embedding(2.0), {"content": "keep old"})

    async def embed_fn(_text: str) -> list[float]:
        return _make_embedding(3.0)

    await vector_store.rebuild(
        [{"id": MEM_2, "content": "keep new"}],
        embed_fn,
        recreate_on_mismatch=True,
    )

    assert set(await vector_store.scroll_ids()) == {MEM_2}
    assert await vector_store.count() == 1


@pytest.mark.asyncio
async def test_rebuild_streaming_stale_delete_strategy_removes_stale_point_ids(
    vector_store: VectorStore,
) -> None:
    await vector_store.upsert(MEM_1, _make_embedding(1.0), {"content": "stale"})
    await vector_store.upsert(MEM_2, _make_embedding(2.0), {"content": "keep old"})

    async def embed_fn(_text: str) -> list[float]:
        return _make_embedding(3.0)

    await vector_store.rebuild(
        [{"id": MEM_2, "content": "keep new"}],
        embed_fn,
        stale_delete_strategy="streaming",
    )

    assert set(await vector_store.scroll_ids()) == {MEM_2}
    assert await vector_store.count() == 1


@pytest.mark.asyncio
async def test_rebuild_same_dimension_holds_lifecycle_lock_for_expensive_work() -> None:
    store = VectorStore(collection_name="mock_memories", embedding_dim=4)
    client = MagicMock()
    client.collection_exists.return_value = True
    client.get_collection.return_value = _collection_info(4)
    store._client = client

    scroll_lock_states: list[bool] = []
    embed_lock_states: list[bool] = []
    upsert_lock_states: list[bool] = []
    delete_lock_states: list[bool] = []

    def scroll(**_kwargs: object) -> tuple[list[SimpleNamespace], None]:
        scroll_lock_states.append(store._collection_lifecycle_lock.locked())
        return ([SimpleNamespace(id=MEM_1)], None)

    def delete(**_kwargs: object) -> None:
        delete_lock_states.append(store._collection_lifecycle_lock.locked())

    client.scroll.side_effect = scroll
    client.delete.side_effect = delete

    async def embed_fn(_text: str) -> list[float]:
        embed_lock_states.append(store._collection_lifecycle_lock.locked())
        return _make_embedding()

    async def batch_upsert(
        _items: list[tuple[str, list[float], dict[str, object]]],
        collection_name: str | None = None,
        *,
        client: object | None = None,
    ) -> None:
        del collection_name, client
        upsert_lock_states.append(store._collection_lifecycle_lock.locked())

    store._queries.batch_upsert = AsyncMock(side_effect=batch_upsert)  # type: ignore[method-assign]

    await store.rebuild([{"id": MEM_2, "content": "keep"}], embed_fn)

    assert scroll_lock_states == [True]
    assert embed_lock_states == [True]
    assert upsert_lock_states == [True]
    assert delete_lock_states == [True]
    client.delete.assert_called_once()


@pytest.mark.asyncio
async def test_rebuild_deletes_stale_point_ids_in_batches_under_lifecycle_lock() -> None:
    store = VectorStore(collection_name="mock_memories", embedding_dim=4)
    client = MagicMock()
    client.collection_exists.return_value = True
    client.get_collection.return_value = _collection_info(4)
    stale_ids = [f"stale-{idx:04d}" for idx in range(1001)]
    client.scroll.return_value = ([SimpleNamespace(id=point_id) for point_id in stale_ids], None)
    store._client = client

    delete_batch_sizes: list[int] = []
    delete_lock_states: list[bool] = []

    def delete(**kwargs: object) -> None:
        selector = kwargs["points_selector"]
        delete_batch_sizes.append(len(selector.points))
        delete_lock_states.append(store._collection_lifecycle_lock.locked())

    client.delete.side_effect = delete

    async def embed_fn(_text: str) -> list[float]:
        return _make_embedding()

    await store.rebuild([], embed_fn, recreate_on_mismatch=True)

    assert delete_batch_sizes == [500, 500, 1]
    assert delete_lock_states == [True, True, True]


@pytest.mark.asyncio
async def test_rebuild_dimension_mismatch_populates_before_atomic_alias_swap() -> None:
    store = VectorStore(collection_name="mock_memories", embedding_dim=4)
    client = MagicMock()
    client.collection_exists.return_value = True
    client.get_collection.return_value = _collection_info(3)
    client.get_aliases.return_value = SimpleNamespace(
        aliases=[SimpleNamespace(alias_name="mock_memories", collection_name="mock_memories@old")]
    )
    store._client = client
    target_name: str | None = None
    populated = False

    def create_collection(*, collection_name: str, vectors_config: VectorParams) -> None:
        nonlocal target_name
        assert collection_name.startswith("mock_memories@rebuild-")
        target_name = collection_name
        assert vectors_config.size == 4
        assert store._collection_lifecycle_lock.locked()

    async def batch_upsert(
        items: list[tuple[str, list[float], dict[str, object]]],
        collection_name: str | None = None,
        **kwargs: object,
    ) -> None:
        nonlocal populated
        assert kwargs["client"] is client
        assert collection_name == target_name
        assert items[0][0] == MEM_1
        client.update_collection_aliases.assert_not_called()
        populated = True

    def update_aliases(*, change_aliases_operations: list[object]) -> None:
        assert populated is True
        assert store._collection_lifecycle_lock.locked()
        assert len(change_aliases_operations) == 2
        assert change_aliases_operations[0].delete_alias.alias_name == "mock_memories"
        assert change_aliases_operations[1].create_alias.collection_name == target_name
        assert change_aliases_operations[1].create_alias.alias_name == "mock_memories"

    client.create_collection.side_effect = create_collection
    client.update_collection_aliases.side_effect = update_aliases
    store._queries.batch_upsert = AsyncMock(side_effect=batch_upsert)  # type: ignore[method-assign]

    async def embed_fn(_text: str) -> list[float]:
        return _make_embedding()

    await store.rebuild([{"id": MEM_1, "content": "one"}], embed_fn)

    client.create_collection.assert_called_once()
    client.update_collection_aliases.assert_called_once()
    client.delete_collection.assert_called_once_with(collection_name="mock_memories@old")


@pytest.mark.asyncio
async def test_dimension_mismatch_recovers_and_supports_writes_and_queries(tmp_path) -> None:
    """A completed rebuild replaces the old collection through a serving alias."""
    # Create a collection with dim=4
    store = VectorStore(
        path=str(tmp_path / "qdrant"),
        collection_name="dim_test",
        embedding_dim=4,
    )
    await store.initialize()
    await store.upsert(MEM_1, _make_embedding(1.0, dim=4), {"content": "test"})
    await store.close()

    # Reopen with a different dimension. Startup keeps the stale collection serving
    # and exposes that existing memories still need to be re-embedded.
    store2 = VectorStore(
        path=str(tmp_path / "qdrant"),
        collection_name="dim_test",
        embedding_dim=768,
    )
    await store2.initialize()

    assert store2.status_snapshot()["state"] == "dimension_mismatch_pending_rebuild"
    assert store2.status_snapshot()["rebuild_required"] is True
    old_results = await store2.search(_make_embedding(1.0, dim=4))
    assert [result[0] for result in old_results] == [MEM_1]

    async def embed_fn(_text: str) -> list[float]:
        return _make_embedding(2.0, dim=768)

    await store2.rebuild([{"id": MEM_2, "content": "new"}], embed_fn)
    assert store2.status_snapshot()["state"] == "ready"
    assert store2.status_snapshot()["rebuild_required"] is False
    results = await store2.search(_make_embedding(2.0, dim=768))
    assert [result[0] for result in results] == [MEM_2]
    aliases = await store2.get_aliases()
    assert aliases["dim_test"].startswith("dim_test@rebuild-")

    await store2.close()


@pytest.mark.asyncio
async def test_dimension_rebuild_failure_keeps_old_collection_serving(tmp_path) -> None:
    store = VectorStore(
        path=str(tmp_path / "qdrant"),
        collection_name="dim_test",
        embedding_dim=4,
    )
    await store.initialize()
    await store.upsert(MEM_1, _make_embedding(1.0, dim=4), {"content": "old"})
    await store.close()

    replacement = VectorStore(
        path=str(tmp_path / "qdrant"),
        collection_name="dim_test",
        embedding_dim=8,
    )
    await replacement.initialize()
    embed_calls = 0

    async def failing_embed(_text: str) -> list[float]:
        nonlocal embed_calls
        embed_calls += 1
        if embed_calls == 2:
            raise RuntimeError("embedding failed")
        return _make_embedding(2.0, dim=8)

    with pytest.raises(RuntimeError, match="embedding failed"):
        await replacement.rebuild(
            [
                {"id": MEM_1, "content": "updated"},
                {"id": MEM_2, "content": "new"},
            ],
            failing_embed,
        )

    old_results = await replacement.search(_make_embedding(1.0, dim=4))
    assert [result[0] for result in old_results] == [MEM_1]
    assert await replacement.get_aliases() == {}
    client = replacement._client
    assert client is not None
    collections = await asyncio.to_thread(client.get_collections)
    assert [collection.name for collection in collections.collections] == ["dim_test"]
    await replacement.close()


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
    assert store._client is None

    result = await store.close()
    assert result is None
    assert store._client is None


@pytest.mark.asyncio
async def test_get_collection_dimension_returns_none_on_client_error(caplog) -> None:
    store = VectorStore(collection_name="test_memories", embedding_dim=4)
    client = MagicMock()
    client.get_collection.side_effect = RuntimeError("boom")
    store._client = client

    dimension = await store.get_collection_dimension()

    assert dimension is None
    assert "Failed to read Qdrant collection dimension" in caplog.text


class TestRemoteTimeoutHint:
    """The remote-call timeout hint must respect qdrant method signatures."""

    def test_accepts_timeout_kwarg_matches_qdrant_signatures(self) -> None:
        from qdrant_client import AsyncQdrantClient

        from gobby.memory.vectorstore_client import _accepts_timeout_kwarg

        assert not _accepts_timeout_kwarg(AsyncQdrantClient.get_collection)
        assert _accepts_timeout_kwarg(AsyncQdrantClient.upsert)
        assert _accepts_timeout_kwarg(AsyncQdrantClient.delete)
        assert _accepts_timeout_kwarg(AsyncQdrantClient.set_payload)
        assert _accepts_timeout_kwarg(AsyncQdrantClient.query_points)
        assert _accepts_timeout_kwarg(AsyncQdrantClient.retrieve)
        assert _accepts_timeout_kwarg(AsyncQdrantClient.count)

    @pytest.mark.asyncio
    async def test_remote_call_injects_timeout_only_where_accepted(self) -> None:
        import time

        from qdrant_client import AsyncQdrantClient, QdrantClient

        from gobby.memory.vectorstore_client import VectorStoreClient

        class _FakeRemoteClient:
            """Mimics qdrant's unknown-kwargs rejection on upsert."""

            def __init__(self) -> None:
                self.calls: list[tuple[str, object]] = []

            async def upsert(self, collection_name: str, points: list, **kwargs: object) -> str:
                if kwargs:
                    raise ValueError(f"Unknown arguments: {list(kwargs.keys())}")
                self.calls.append(("upsert", None))
                return "ok"

            async def get_collection(self, collection_name: str, **kwargs: object) -> str:
                if kwargs:
                    raise ValueError(f"Unknown arguments: {list(kwargs.keys())}")
                self.calls.append(("get_collection", None))
                return "ok"

            async def query_points(
                self, collection_name: str, timeout: int | None = None, **kwargs: object
            ) -> str:
                self.calls.append(("query_points", timeout))
                return "ok"

        ops = VectorStoreClient(
            SimpleNamespace(_url="http://qdrant:6333"),
            time.monotonic,
            local_client_factory=QdrantClient,
            remote_client_factory=lambda **kwargs: None,
        )
        fake = _FakeRemoteClient()
        client = cast(AsyncQdrantClient, fake)

        assert await ops.call(client, "upsert", collection_name="c", points=[]) == "ok"
        assert await ops.call(client, "get_collection", collection_name="c") == "ok"
        assert await ops.call(client, "query_points", collection_name="c") == "ok"
        assert fake.calls == [
            ("upsert", None),
            ("get_collection", None),
            ("query_points", 5),
        ]
