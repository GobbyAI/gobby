"""Tests for MemoryManager integration with VectorStore.

Validates that create_memory, search_memories, delete_memory, and
update_memory correctly interact with both SQLite and VectorStore.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.manager import MemoryManager
from gobby.memory.vectorstore import VectorStore
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path):
    """Create a temporary database for testing."""
    database = LocalDatabase(tmp_path / "gobby-hub.db")
    run_migrations(database)
    yield database
    database.close()


@pytest.fixture
def mock_vector_store():
    """Create a mock VectorStore."""
    vs = AsyncMock(spec=VectorStore)
    vs.upsert = AsyncMock()
    vs.search = AsyncMock(return_value=[])
    vs.delete = AsyncMock()
    vs.count = AsyncMock(return_value=0)
    vs.batch_upsert = AsyncMock()
    return vs


@pytest.fixture
def mock_embed_fn():
    """Create a mock embedding function."""
    return AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4] * 384)  # 1536-dim


@pytest.fixture
def manager(db, mock_vector_store, mock_embed_fn):
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
async def test_create_memory_upserts_to_qdrant(manager, mock_vector_store, mock_embed_fn):
    """create_memory should store in SQLite AND upsert to Qdrant."""
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


@pytest.mark.asyncio
async def test_create_memory_works_without_vectorstore(db):
    """create_memory should work when VectorStore is None (Phase 1 compat)."""
    config = MemoryConfig(enabled=True, backend="local")
    mgr = MemoryManager(db=db, config=config)

    memory = await mgr.create_memory(content="no vector store")
    assert memory.content == "no vector store"


@pytest.mark.asyncio
async def test_search_memories_queries_qdrant(manager, mock_vector_store, mock_embed_fn):
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
async def test_search_memories_user_source_boost(manager, mock_vector_store, mock_embed_fn):
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

    # Suppress FTS5 so RRF ranking is purely from Qdrant — isolates the boost test
    manager._fts_searcher = AsyncMock()
    manager._fts_searcher.search = lambda *a, **kw: []

    results = await manager.search_memories(query="alpha", limit=10)

    # Both should be present
    assert len(results) == 2
    result_ids = [r.id for r in results]

    # User memory should be boosted and appear before agent memory
    user_idx = result_ids.index(user_mem.id)
    agent_idx = result_ids.index(agent_mem.id)
    assert user_idx < agent_idx, "User memory should rank higher due to user_source_boost"


@pytest.mark.asyncio
async def test_search_memories_no_query_returns_list(manager, mock_vector_store):
    """search_memories without query should list from SQLite."""
    await manager.create_memory(content="fact one")
    await manager.create_memory(content="fact two")

    # Reset search mock after create_memory (dedup may call search)
    mock_vector_store.search.reset_mock()

    results = await manager.search_memories(query=None, limit=10)

    # Should NOT call VectorStore search for query=None
    mock_vector_store.search.assert_not_awaited()
    assert len(results) == 2


@pytest.mark.asyncio
async def test_delete_memory_removes_from_qdrant(manager, mock_vector_store):
    """delete_memory should remove from both SQLite and Qdrant."""
    memory = await manager.create_memory(content="to delete")
    await manager.delete_memory(memory.id)

    # Should have deleted from VectorStore
    mock_vector_store.delete.assert_awaited_once_with(memory.id)


@pytest.mark.asyncio
async def test_delete_memory_removes_from_graph(manager, mock_vector_store):
    """delete_memory should remove from Neo4j when kg_service is available."""
    mock_kg = AsyncMock()
    mock_kg.remove_memory_from_graph = AsyncMock()
    manager._kg_service = mock_kg

    memory = await manager.create_memory(content="to delete from graph")
    await manager.delete_memory(memory.id)

    mock_kg.remove_memory_from_graph.assert_awaited_once_with(memory.id, project_id=None)


@pytest.mark.asyncio
async def test_delete_memory_works_without_kg_service(manager, mock_vector_store):
    """delete_memory should succeed when _kg_service is None."""
    manager._kg_service = None
    memory = await manager.create_memory(content="no graph")
    result = await manager.delete_memory(memory.id)
    assert result is True


@pytest.mark.asyncio
async def test_update_memory_re_embeds(manager, mock_vector_store, mock_embed_fn):
    """update_memory should re-embed and upsert to Qdrant when content changes."""
    memory = await manager.create_memory(content="original")
    mock_embed_fn.reset_mock()
    mock_vector_store.upsert.reset_mock()

    await manager.update_memory(memory.id, content="updated content")

    # Should have re-embedded
    mock_embed_fn.assert_awaited_once_with("updated content")
    # Should have upserted to Qdrant
    mock_vector_store.upsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_memories_tag_filtering(manager, mock_vector_store, mock_embed_fn):
    """search_memories should support tag filtering."""
    m1 = await manager.create_memory(content="tagged", tags=["python"])
    await manager.create_memory(content="untagged")
    mock_embed_fn.reset_mock()

    # Search with tags_all filter (no query = SQLite list)
    results = await manager.search_memories(query=None, tags_all=["python"])
    assert len(results) == 1
    assert results[0].id == m1.id


@pytest.mark.asyncio
async def test_search_memories_skips_deleted_memories(manager, mock_vector_store, mock_embed_fn):
    """search_memories should skip memories deleted from DB but still in the index."""
    # Create two memories, then delete one from DB only (index still references it)
    mem_kept = await manager.create_memory(content="still here")
    mem_deleted = await manager.create_memory(content="will be deleted")
    mock_embed_fn.reset_mock()

    # Delete from DB directly (simulating index/DB desync)
    manager.storage.db.execute("DELETE FROM memories WHERE id = ?", (mem_deleted.id,))

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
async def test_no_search_coordinator_import():
    """MemoryManager should not import SearchCoordinator."""
    import gobby.memory.manager as mod

    source = open(mod.__file__).read()
    assert "SearchCoordinator" not in source
    assert "EmbeddingService" not in source
    assert "MemoryEmbeddingManager" not in source
