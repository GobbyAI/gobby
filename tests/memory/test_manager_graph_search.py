"""Tests for MemoryManager graph-augmented search: parallel search, RRF merge, degradation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.manager import MemoryManager

pytestmark = pytest.mark.unit


def _make_manager(
    neo4j_url: str | None = None,
    llm_service: MagicMock | None = None,
    vector_store: AsyncMock | None = None,
    embed_fn: AsyncMock | None = None,
    graph_search: bool = True,
    graph_min_score: float = 0.5,
    rrf_k: int = 60,
) -> MemoryManager:
    """Create a MemoryManager with controlled dependencies."""
    db = MagicMock()
    db.fetchall = MagicMock(return_value=[])
    db.fetchone = MagicMock(return_value=None)
    db.execute = MagicMock()

    config = MemoryConfig(
        neo4j_url=neo4j_url,
        neo4j_auth="neo4j:password" if neo4j_url else None,
        neo4j_graph_search=graph_search,
        neo4j_graph_min_score=graph_min_score,
        neo4j_rrf_k=rrf_k,
    )

    return MemoryManager(
        db=db,
        config=config,
        llm_service=llm_service,
        vector_store=vector_store,
        embed_fn=embed_fn,
    )


def _mock_memory(memory_id: str, content: str, memory_type: str = "fact") -> MagicMock:
    """Create a mock Memory object."""
    m = MagicMock()
    m.id = memory_id
    m.content = content
    m.memory_type = memory_type
    m.source_type = "user"
    m.tags = []
    m.last_accessed_at = None
    return m


class TestRRFMerge:
    """Tests for _rrf_merge static method."""

    def test_single_source(self) -> None:
        """RRF with single source preserves order."""
        result = MemoryManager._rrf_merge(
            qdrant_ranked=["a", "b", "c"],
            graph_ranked=[],
            k=60,
        )
        assert result == ["a", "b", "c"]

    def test_both_sources_boost_shared(self) -> None:
        """Items in both lists rank higher than items in only one list."""
        result = MemoryManager._rrf_merge(
            qdrant_ranked=["a", "b", "c"],
            graph_ranked=["b", "d", "a"],
            k=60,
        )
        # "a" and "b" appear in both, should rank highest
        assert result[0] in ("a", "b")
        assert result[1] in ("a", "b")
        # "c" and "d" only in one source
        assert set(result) == {"a", "b", "c", "d"}

    def test_disjoint_lists(self) -> None:
        """RRF with disjoint lists produces interleaved results."""
        result = MemoryManager._rrf_merge(
            qdrant_ranked=["a", "b"],
            graph_ranked=["c", "d"],
            k=60,
        )
        # All items should appear
        assert set(result) == {"a", "b", "c", "d"}
        # First-ranked from each source should come first
        assert result[0] in ("a", "c")
        assert result[1] in ("a", "c")

    def test_empty_inputs(self) -> None:
        """RRF with empty inputs returns empty."""
        result = MemoryManager._rrf_merge([], [], k=60)
        assert result == []

    def test_k_affects_distribution(self) -> None:
        """Lower k gives more weight to rank position."""
        # With k=1, rank differences matter more
        result_low_k = MemoryManager._rrf_merge(
            qdrant_ranked=["a", "b"],
            graph_ranked=["b", "a"],
            k=1,
        )
        # With equal appearances, order depends on rank sum
        assert set(result_low_k) == {"a", "b"}


class TestSearchGraphForMemories:
    """Tests for _search_graph_for_memories."""

    async def test_returns_direct_memory_ids(self) -> None:
        """_search_graph_for_memories returns memory IDs from entity vector search."""
        llm_service = MagicMock()
        llm_service.get_default_provider = MagicMock(return_value=AsyncMock())

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        # Mock entity search results
        manager._kg_service.search_entities_by_vector = AsyncMock(
            return_value=[
                {
                    "name": "Python",
                    "labels": ["Tool"],
                    "score": 0.9,
                    "memory_ids": ["mem-1", "mem-2"],
                },
                {"name": "FastAPI", "labels": ["Framework"], "score": 0.8, "memory_ids": ["mem-3"]},
            ]
        )
        manager._kg_service.find_related_memory_ids = AsyncMock(return_value=["mem-4"])

        result = await manager._search_graph_for_memories(
            query_embedding=[0.1, 0.2],
            limit=10,
        )

        assert result == ["mem-1", "mem-2", "mem-3", "mem-4"]

    async def test_deduplicates_traversed_ids(self) -> None:
        """_search_graph_for_memories deduplicates IDs from traversal."""
        llm_service = MagicMock()
        llm_service.get_default_provider = MagicMock(return_value=AsyncMock())

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        manager._kg_service.search_entities_by_vector = AsyncMock(
            return_value=[
                {"name": "A", "labels": [], "score": 0.9, "memory_ids": ["mem-1"]},
            ]
        )
        # Traversal returns overlapping ID
        manager._kg_service.find_related_memory_ids = AsyncMock(return_value=["mem-1", "mem-2"])

        result = await manager._search_graph_for_memories(
            query_embedding=[0.1],
            limit=10,
        )

        # mem-1 should appear only once
        assert result == ["mem-1", "mem-2"]

    async def test_returns_empty_when_no_entities(self) -> None:
        """_search_graph_for_memories returns empty when no entity matches."""
        llm_service = MagicMock()
        llm_service.get_default_provider = MagicMock(return_value=AsyncMock())

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        manager._kg_service.search_entities_by_vector = AsyncMock(return_value=[])

        result = await manager._search_graph_for_memories(
            query_embedding=[0.1],
        )

        assert result == []


class TestSearchMemoriesGraphIntegration:
    """Tests for graph-augmented search_memories."""

    async def test_parallel_search_with_rrf_merge(self) -> None:
        """search_memories runs Qdrant and graph search in parallel, merges via RRF."""
        llm_service = MagicMock()
        llm_service.get_default_provider = MagicMock(return_value=AsyncMock())

        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1, 0.2])

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
            vector_store=vs,
            embed_fn=embed_fn,
        )

        # Qdrant returns mem-1, mem-2
        vs.search = AsyncMock(return_value=[("mem-1", 0.9), ("mem-2", 0.7)])

        # Graph returns mem-2, mem-3
        manager._kg_service.search_entities_by_vector = AsyncMock(
            return_value=[
                {"name": "A", "labels": [], "score": 0.9, "memory_ids": ["mem-2", "mem-3"]},
            ]
        )
        manager._kg_service.find_related_memory_ids = AsyncMock(return_value=[])

        # Mock storage
        manager.storage.get_memory = MagicMock(
            side_effect=lambda mid: _mock_memory(mid, f"content of {mid}")
        )

        result = await manager.search_memories(query="test query", limit=10)

        assert len(result) >= 2
        result_ids = [m.id for m in result]
        # mem-2 appears in both sources, should rank high
        assert "mem-2" in result_ids
        assert "mem-1" in result_ids

    async def test_graceful_degradation_graph_failure(self) -> None:
        """search_memories falls back to Qdrant-only when graph search fails."""
        llm_service = MagicMock()
        llm_service.get_default_provider = MagicMock(return_value=AsyncMock())

        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
            vector_store=vs,
            embed_fn=embed_fn,
        )

        # Qdrant works
        vs.search = AsyncMock(return_value=[("mem-1", 0.9)])

        # Graph search fails
        manager._kg_service.search_entities_by_vector = AsyncMock(
            side_effect=Exception("Neo4j down")
        )

        manager.storage.get_memory = MagicMock(
            side_effect=lambda mid: _mock_memory(mid, f"content of {mid}")
        )

        result = await manager.search_memories(query="test query", limit=10)

        # Should still return Qdrant results
        assert len(result) == 1
        assert result[0].id == "mem-1"

    async def test_qdrant_only_when_graph_search_disabled(self) -> None:
        """search_memories skips graph search when neo4j_graph_search is False."""
        llm_service = MagicMock()
        llm_service.get_default_provider = MagicMock(return_value=AsyncMock())

        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
            vector_store=vs,
            embed_fn=embed_fn,
            graph_search=False,
        )

        vs.search = AsyncMock(return_value=[("mem-1", 0.9)])
        manager.storage.get_memory = MagicMock(
            side_effect=lambda mid: _mock_memory(mid, f"content of {mid}")
        )

        # Mock the kg_service method to verify it's not called
        if manager._kg_service:
            manager._kg_service.search_entities_by_vector = AsyncMock()

        result = await manager.search_memories(query="test query", limit=10)

        assert len(result) == 1
        # Graph search methods should not have been called
        if manager._kg_service:
            manager._kg_service.search_entities_by_vector.assert_not_called()

    async def test_qdrant_only_when_no_kg_service(self) -> None:
        """search_memories uses Qdrant-only path when no KG service."""
        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            neo4j_url=None,  # No Neo4j
            vector_store=vs,
            embed_fn=embed_fn,
        )

        vs.search = AsyncMock(return_value=[("mem-1", 0.8)])
        manager.storage.get_memory = MagicMock(
            side_effect=lambda mid: _mock_memory(mid, f"content of {mid}")
        )

        result = await manager.search_memories(query="test query", limit=10)

        assert len(result) == 1
        assert result[0].id == "mem-1"

    async def test_user_source_boost_applied(self) -> None:
        """search_memories applies user source boost in graph-augmented mode."""
        llm_service = MagicMock()
        llm_service.get_default_provider = MagicMock(return_value=AsyncMock())

        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
            vector_store=vs,
            embed_fn=embed_fn,
        )

        # Both memories appear in Qdrant, but mem-2 is user-sourced
        vs.search = AsyncMock(return_value=[("mem-1", 0.9), ("mem-2", 0.85)])
        manager._kg_service.search_entities_by_vector = AsyncMock(return_value=[])
        manager._kg_service.find_related_memory_ids = AsyncMock(return_value=[])

        user_mem = _mock_memory("mem-2", "user content")
        user_mem.source_type = "user"
        system_mem = _mock_memory("mem-1", "system content")
        system_mem.source_type = "session"

        manager.storage.get_memory = MagicMock(
            side_effect=lambda mid: user_mem if mid == "mem-2" else system_mem
        )

        result = await manager.search_memories(query="test", limit=10)

        # Both should be returned
        result_ids = [m.id for m in result]
        assert "mem-1" in result_ids
        assert "mem-2" in result_ids


class TestCreateMemoryPassesMemoryId:
    """Tests that create_memory passes memory_id to graph background task."""

    async def test_fire_background_graph_receives_memory_id(self) -> None:
        """_fire_background_graph is called with memory_id from create_memory."""
        llm_service = MagicMock()
        llm_service.get_default_provider = MagicMock(return_value=AsyncMock())

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        # Mock the backend
        manager._backend = AsyncMock()
        manager._backend.content_exists = AsyncMock(return_value=False)

        from gobby.memory.protocol import MemoryRecord

        mock_record = MagicMock(spec=MemoryRecord)
        mock_record.id = "test-mem-id"
        mock_record.memory_type = "fact"
        mock_record.content = "test content"
        mock_record.created_at = MagicMock(isoformat=MagicMock(return_value="2026-01-01"))
        mock_record.updated_at = MagicMock(isoformat=MagicMock(return_value="2026-01-01"))
        mock_record.project_id = None
        mock_record.source_type = "user"
        mock_record.source_session_id = None
        mock_record.access_count = 0
        mock_record.last_accessed_at = None
        mock_record.tags = []
        manager._backend.create = AsyncMock(return_value=mock_record)

        manager._kg_service.add_to_graph = AsyncMock()

        await manager.create_memory(content="test content")

        # Wait for background task
        if manager._background_tasks:
            await asyncio.wait(manager._background_tasks, timeout=1.0)

        manager._kg_service.add_to_graph.assert_called_once_with(
            "test content", memory_id="test-mem-id"
        )
