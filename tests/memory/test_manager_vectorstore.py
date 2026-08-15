"""Tests for MemoryManager integration with VectorStore.

Validates that create_memory, search_memories, delete_memory, and
update_memory correctly interact with local storage and VectorStore.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.manager import MemoryManager
from gobby.memory.vectorstore import VectorStore
from gobby.storage.embedding_generation_state import EmbeddingGenerationLeaseLost
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID

pytestmark = pytest.mark.unit


async def test_search_by_stored_vectors() -> None:
    store = VectorStore(url="http://qdrant:6333", collection_name="stored-search")
    client = MagicMock()
    records = [
        SimpleNamespace(id=f"memory-{index}", vector=[float(index), 1.0]) for index in range(51)
    ]
    client.retrieve = AsyncMock(return_value=records)

    async def query_batch_points(**kwargs: object) -> list[SimpleNamespace]:
        requests = kwargs["requests"]
        assert isinstance(requests, list)
        return [
            SimpleNamespace(points=[SimpleNamespace(id="neighbor", score=0.8)])
            for _request in requests
        ]

    client.query_batch_points = AsyncMock(side_effect=query_batch_points)
    client.close = AsyncMock()
    store._client = client

    result = await store.search_by_stored_vectors(
        [*[f"memory-{index}" for index in range(51)], "missing"],
        limit=3,
        timeout=2.5,
    )
    await store.close()

    assert result["memory-0"] == [("neighbor", 0.8)]
    assert "missing" not in result
    client.retrieve.assert_awaited_once()
    assert client.query_batch_points.await_count == 2
    assert isinstance(client.retrieve.await_args.kwargs["timeout"], int)
    assert client.retrieve.await_args.kwargs["timeout"] > 0
    client.close.assert_awaited_once()


@pytest.fixture
def db(hub_db: HubDatabase) -> HubDatabase:
    """Create a temporary hub database for testing."""
    return hub_db


@pytest.fixture
def mock_vector_store() -> AsyncMock:
    """Create a mock VectorStore."""
    vs = AsyncMock(spec=VectorStore)
    vs.upsert = AsyncMock()
    vs.search = AsyncMock(return_value=[])
    vs.search_by_stored_vectors = AsyncMock(return_value={})
    vs.delete = AsyncMock()
    vs.count = AsyncMock(return_value=0)
    vs.batch_upsert = AsyncMock()
    return vs


@pytest.fixture
def mock_embed_fn() -> AsyncMock:
    """Create a mock embedding function."""
    return AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4] * 384)  # 1536-dim


@pytest.fixture
def manager(db, mock_vector_store: AsyncMock, mock_embed_fn: AsyncMock) -> MemoryManager:
    """Create a MemoryManager with VectorStore."""
    config = MemoryConfig(enabled=True, backend="local")
    mgr = MemoryManager(
        db=db,
        config=config,
        vector_store=mock_vector_store,
        embed_fn=mock_embed_fn,
    )
    return mgr


@pytest.mark.asyncio
async def test_create_memory_upserts_to_qdrant(
    manager: MemoryManager, mock_vector_store: AsyncMock, mock_embed_fn: AsyncMock
) -> None:
    """create_memory should store locally and upsert to Qdrant."""
    memory = await manager.create_memory(
        content="test fact",
        memory_type="fact",
    )

    # Should have called embed_fn
    mock_embed_fn.assert_awaited_once_with("test fact")

    # Should have upserted to VectorStore
    mock_vector_store.upsert.assert_awaited_once()
    call_args = mock_vector_store.upsert.call_args
    assert call_args[0][0] == memory.id  # memory_id
    assert "project_id" in call_args[0][2]  # payload has project_id


async def test_fenced_lease_upsert_preserves_durable_reindex_intent(
    manager: MemoryManager,
    mock_vector_store: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_vector_store.upsert.side_effect = EmbeddingGenerationLeaseLost(
        "Embedding generation serving is fenced"
    )

    with caplog.at_level(logging.WARNING):
        memory = await manager.create_memory(content="retry vector indexing after recovery")

    stored = manager.get_memory(memory.id)
    assert stored is not None
    assert stored.vector_needs_reindex is True
    assert "VectorStore upsert unavailable" in caplog.text
    assert "VectorStore upsert failed" not in caplog.text


@pytest.mark.asyncio
async def test_create_memory_works_without_vectorstore(db: HubDatabase) -> None:
    """create_memory should work when VectorStore is None (Phase 1 compat)."""
    config = MemoryConfig(enabled=True, backend="local")
    mgr = MemoryManager(db=db, config=config)

    memory = await mgr.create_memory(content="no vector store")
    assert memory.content == "no vector store"


@pytest.mark.asyncio
async def test_search_memories_queries_qdrant(
    manager: MemoryManager, mock_vector_store: AsyncMock, mock_embed_fn: AsyncMock
) -> None:
    """search_memories with query should embed query + search Qdrant."""
    # Create a memory first
    memory = await manager.create_memory(content="cats are great")

    # Reset mocks after create to isolate search behavior
    mock_embed_fn.reset_mock()
    mock_vector_store.search.reset_mock()

    # Setup mock search results — Qdrant returns our memory
    mock_vector_store.search.return_value = [(memory.id, 0.95)]

    results = await manager.search_memories(query="cats", limit=5)

    # Should have called embed_fn with query text and is_query=True
    embed_calls = mock_embed_fn.call_args_list
    query_calls = [
        c
        for c in embed_calls
        if c.kwargs.get("is_query") is True or (len(c.args) >= 2 and c.args[1] is True)
    ]
    assert len(query_calls) >= 1

    # Should have searched VectorStore (Qdrant)
    assert mock_vector_store.search.await_count >= 1

    # Should return resolved Memory objects
    assert len(results) >= 1
    assert any(r.content == "cats are great" for r in results)


@pytest.mark.asyncio
async def test_search_memories_user_source_boost(
    manager: MemoryManager, mock_vector_store: AsyncMock, mock_embed_fn: AsyncMock
) -> None:
    """search_memories should boost user memories by 1.2x."""
    # Create two memories
    user_mem = await manager.create_memory(content="user memory alpha", source_type="user")
    agent_mem = await manager.create_memory(content="agent memory alpha", source_type="agent")
    mock_embed_fn.reset_mock()
    mock_vector_store.search.reset_mock()

    # Both returned with same score — agent first in raw Qdrant ranking
    mock_vector_store.search.return_value = [
        (agent_mem.id, 0.8),
        (user_mem.id, 0.8),
    ]

    # Suppress keyword search so RRF ranking is purely from Qdrant — isolates the boost test
    manager._search_service._keyword_ranked = AsyncMock(return_value=[])

    results = await manager.search_memories(query="alpha", limit=10)

    # Both should be present
    assert len(results) == 2
    result_ids = [r.id for r in results]

    # User memory should be boosted and appear before agent memory
    user_idx = result_ids.index(user_mem.id)
    agent_idx = result_ids.index(agent_mem.id)
    assert user_idx < agent_idx, "User memory should rank higher due to user_source_boost"


@pytest.mark.asyncio
async def test_search_memories_no_query_returns_list(
    manager: MemoryManager, mock_vector_store: AsyncMock
) -> None:
    """search_memories without query should list from local storage."""
    await manager.create_memory(content="fact one")
    await manager.create_memory(content="fact two")

    # Reset search mock after create_memory (dedup may call search)
    if manager._background_tasks:
        await asyncio.wait_for(
            asyncio.gather(*list(manager._background_tasks)),
            timeout=5,
        )
    mock_vector_store.search.reset_mock()

    results = await manager.search_memories(query=None, limit=10)

    # Should NOT call VectorStore search for query=None
    mock_vector_store.search.assert_not_awaited()
    assert len(results) == 2


@pytest.mark.asyncio
async def test_delete_memory_removes_from_qdrant(
    manager: MemoryManager, mock_vector_store: AsyncMock
) -> None:
    """delete_memory should remove from both local storage and Qdrant."""
    memory = await manager.create_memory(content="to delete")
    await manager.delete_memory(memory.id)

    # Should have deleted from VectorStore
    mock_vector_store.delete.assert_awaited_once_with(memory.id)
    assert mock_vector_store.delete.await_count == 1
    assert mock_vector_store.delete.await_args is not None


async def test_fenced_lease_purge_uses_rate_limited_availability_reporting(
    manager: MemoryManager,
    mock_vector_store: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = EmbeddingGenerationLeaseLost("Embedding generation serving is fenced")
    availability_logger = MagicMock()
    manager._lifecycle_service._log_vector_store_failure = availability_logger
    mock_vector_store.delete.side_effect = error

    with caplog.at_level(logging.WARNING, logger="gobby.memory.services.lifecycle"):
        await manager._lifecycle_service.purge_secondary_indices("missing-memory")

    availability_logger.assert_called_once_with(
        "VectorStore purge unavailable for missing-memory", error
    )
    assert "VectorStore purge failed" not in caplog.text


@pytest.mark.asyncio
async def test_delete_memory_removes_from_graph(
    manager: MemoryManager, mock_vector_store: AsyncMock
) -> None:
    """delete_memory should remove from Neo4j when kg_service is available."""
    mock_kg = AsyncMock()
    mock_kg.remove_memory_from_graph = AsyncMock()
    manager._kg_service = mock_kg

    memory = await manager.create_memory(content="to delete from graph")
    await manager.delete_memory(memory.id)

    mock_kg.remove_memory_from_graph.assert_awaited_once_with(
        memory.id,
        project_id=PERSONAL_PROJECT_ID,
        is_global=False,
    )
    assert mock_kg.remove_memory_from_graph.await_count == 1
    assert mock_kg.remove_memory_from_graph.await_args is not None


@pytest.mark.asyncio
async def test_delete_memory_works_without_kg_service(
    manager: MemoryManager, mock_vector_store: AsyncMock
) -> None:
    """delete_memory should succeed when _kg_service is None."""
    manager._kg_service = None
    memory = await manager.create_memory(content="no graph")
    result = await manager.delete_memory(memory.id)
    assert result is True


@pytest.mark.asyncio
async def test_update_memory_content_reembeds_same_id(
    manager: MemoryManager, mock_vector_store: AsyncMock, mock_embed_fn: AsyncMock
) -> None:
    memory = await manager.create_memory(content="original")
    if manager._background_tasks:
        await asyncio.gather(*tuple(manager._background_tasks))
    mock_embed_fn.reset_mock()
    mock_vector_store.upsert.reset_mock()

    updated = await manager.update_memory(memory.id, content="updated content")

    assert updated.id == memory.id
    assert updated.content == "updated content"
    mock_embed_fn.assert_awaited_once_with("updated content")
    mock_vector_store.upsert.assert_awaited_once_with(
        memory.id,
        [0.1, 0.2, 0.3, 0.4] * 384,
        {
            "project_id": PERSONAL_PROJECT_ID,
            "is_global": False,
            "memory_type": "fact",
        },
    )


@pytest.mark.asyncio
async def test_update_memory_type_syncs_qdrant_payload_without_reembedding(
    manager: MemoryManager,
    mock_vector_store: AsyncMock,
    mock_embed_fn: AsyncMock,
) -> None:
    memory = await manager.create_memory(content="typed payload")
    await asyncio.gather(*tuple(manager._background_tasks))
    mock_vector_store.set_payload.reset_mock()
    mock_embed_fn.reset_mock()

    updated = await manager.update_memory(memory.id, memory_type="pattern")

    mock_vector_store.set_payload.assert_awaited_once_with(
        memory.id,
        {
            "project_id": PERSONAL_PROJECT_ID,
            "is_global": False,
            "memory_type": "pattern",
        },
    )
    mock_embed_fn.assert_not_awaited()
    assert updated.memory_type == "pattern"
    assert updated.vector_needs_reindex is False


@pytest.mark.asyncio
async def test_search_memories_tag_filtering(
    manager: MemoryManager, mock_vector_store: AsyncMock, mock_embed_fn: AsyncMock
) -> None:
    """search_memories should support tag filtering."""
    m1 = await manager.create_memory(content="tagged", tags=["python"])
    await manager.create_memory(content="untagged")
    mock_embed_fn.reset_mock()

    # Search with tags_all filter (no query = local storage list)
    results = await manager.search_memories(query=None, tags_all=["python"])
    assert len(results) == 1
    assert results[0].id == m1.id


@pytest.mark.asyncio
async def test_search_memories_skips_deleted_memories(
    manager: MemoryManager, mock_vector_store: AsyncMock, mock_embed_fn: AsyncMock
) -> None:
    """search_memories should skip memories deleted from DB but still in the index."""
    # Create two memories, then delete one from DB only (index still references it)
    mem_kept = await manager.create_memory(content="still here")
    mem_deleted = await manager.create_memory(content="will be deleted")
    mock_embed_fn.reset_mock()

    # Delete from DB directly (simulating index/DB desync)
    manager.storage.db.execute("DELETE FROM memories WHERE id = %s", (mem_deleted.id,))

    # Index returns both IDs (stale reference)
    mock_vector_store.search.return_value = [
        (mem_deleted.id, 0.95),
        (mem_kept.id, 0.90),
    ]

    results = await manager.search_memories(query="test", limit=10)

    # Should return only the existing memory, not crash
    assert len(results) == 1
    assert results[0].id == mem_kept.id


@pytest.mark.asyncio
async def test_purge_dream_hidden_reconciles_qdrant_and_graph(
    manager: MemoryManager, mock_vector_store: AsyncMock
) -> None:
    """Purging an aged soft-hidden row hard-deletes it and reconciles secondary stores.

    Secondary stores keep soft-hidden rows until purge, so the purge path must drop the
    purged memory's Qdrant vector and FalkorDB graph artifacts (#17162). Rows still
    inside their grace window are left untouched.
    """
    mock_kg = AsyncMock()
    mock_kg.remove_memory_from_graph = AsyncMock()
    manager._kg_service = mock_kg

    aged = await manager.create_memory(content="obsolete fact")
    fresh = await manager.create_memory(content="recently hidden fact")

    old_stamp = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    now_stamp = datetime.now(UTC).isoformat()
    manager.mark_dreamed(aged.id, hidden_as="delete", when=old_stamp)
    manager.mark_dreamed(fresh.id, hidden_as="delete", when=now_stamp)

    mock_vector_store.delete.reset_mock()

    result = await manager.purge_dream_hidden("delete", older_than_days=30)

    assert result["purged"] == 1
    assert result["memory_ids"] == [aged.id]
    # Only the aged row is reconciled out of Qdrant and the graph.
    mock_vector_store.delete.assert_awaited_once_with(aged.id)
    mock_kg.remove_memory_from_graph.assert_awaited_once_with(
        aged.id,
        project_id=None,
        is_global=None,
    )
    # The fresh soft-hidden row survives purge (still recoverable).
    assert manager.get_memory(fresh.id, visibility="hidden") is not None
    assert manager.get_memory(aged.id, visibility="all") is None


@pytest.mark.asyncio
async def test_purge_dream_hidden_reconciles_secondary_indices_concurrently(
    manager: MemoryManager,
) -> None:
    """Purged memory IDs reconcile secondary stores concurrently."""
    old_stamp = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    first = await manager.create_memory(content="obsolete fact one")
    second = await manager.create_memory(content="obsolete fact two")
    manager.mark_dreamed(first.id, hidden_as="delete", when=old_stamp)
    manager.mark_dreamed(second.id, hidden_as="delete", when=old_stamp)

    release = asyncio.Event()
    running = 0
    max_running = 0
    calls: list[str] = []

    async def purge_secondary_indices(memory_id: str) -> None:
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        if running == 2:
            release.set()
        await release.wait()
        calls.append(memory_id)
        running -= 1

    manager._lifecycle_service.purge_secondary_indices = AsyncMock(
        side_effect=purge_secondary_indices
    )

    result = await asyncio.wait_for(
        manager.purge_dream_hidden("delete", older_than_days=30),
        timeout=1,
    )

    assert result["purged"] == 2
    assert set(calls) == {first.id, second.id}
    assert max_running == 2


@pytest.mark.asyncio
async def test_restore_memory_unhides_dream_flagged_row(manager: MemoryManager) -> None:
    """Restoring a soft-hidden memory returns it to active visibility.

    Secondary stores keep the row through soft-hide, so restore only flips the
    primary row's visibility — the memory reappears in active reads with its
    ``dream_action`` cleared.
    """
    memory = await manager.create_memory(content="still true fact")
    manager.mark_dreamed(memory.id, hidden_as="review")

    # Hidden: invisible to active reads, present under the hidden scope.
    assert manager.get_memory(memory.id) is None
    assert manager.get_memory(memory.id, visibility="hidden") is not None

    assert manager.restore_memory(memory.id) is True

    restored = manager.get_memory(memory.id)
    assert restored is not None
    assert restored.id == memory.id
    assert manager.count_memories(visibility="hidden") == 0


@pytest.mark.asyncio
async def test_restore_memory_missing_raises(manager: MemoryManager) -> None:
    """Restoring an unknown memory id raises ValueError from storage."""
    with pytest.raises(ValueError, match="not found"):
        # memories.id is a native uuid column, so the unknown probe must be
        # a valid-format UUID.
        manager.restore_memory("99999999-9999-4999-8999-999999999999")


@pytest.mark.asyncio
async def test_no_search_coordinator_import() -> None:
    """MemoryManager should not import SearchCoordinator."""
    import gobby.memory.manager as mod

    source = open(mod.__file__).read()
    assert "SearchCoordinator" not in source
    assert "EmbeddingService" not in source
    assert "MemoryEmbeddingManager" not in source
