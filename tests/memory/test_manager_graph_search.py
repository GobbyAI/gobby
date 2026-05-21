"""Tests for MemoryManager graph-augmented search: parallel search, RRF merge, degradation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.identity import entity_key
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

    config = MemoryConfig()

    return MemoryManager(
        db=db,
        config=config,
        llm_service=llm_service,
        vector_store=vector_store,
        embed_fn=embed_fn,
        neo4j_url=neo4j_url,
        neo4j_auth="neo4j:password" if neo4j_url else None,
        neo4j_graph_search=graph_search,
        neo4j_graph_min_score=graph_min_score,
        neo4j_rrf_k=rrf_k,
    )


def _mock_memory(
    memory_id: str,
    content: str,
    memory_type: str = "fact",
    updated_at: str | None = None,
) -> MagicMock:
    """Create a mock Memory object."""
    m = MagicMock()
    m.id = memory_id
    m.content = content
    m.memory_type = memory_type
    m.source_type = "user"
    m.tags = []
    m.last_accessed_at = None
    m.updated_at = updated_at or datetime.now(UTC).isoformat()
    return m


class TestRRFMerge:
    """Tests for _rrf_merge static method."""

    def test_single_source(self) -> None:
        """RRF with single source preserves order."""
        result = MemoryManager._rrf_merge(
            ["a", "b", "c"],
            [],
            k=60,
        )
        assert result == ["a", "b", "c"]

    def test_both_sources_boost_shared(self) -> None:
        """Items in both lists rank higher than items in only one list."""
        result = MemoryManager._rrf_merge(
            ["a", "b", "c"],
            ["b", "d", "a"],
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
            ["a", "b"],
            ["c", "d"],
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
            ["a", "b"],
            ["b", "a"],
            k=1,
        )
        # With equal appearances, order depends on rank sum
        assert set(result_low_k) == {"a", "b"}

    def test_three_sources(self) -> None:
        """RRF with three sources (Qdrant + graph + FTS5)."""
        result = MemoryManager._rrf_merge(
            ["a", "b"],
            ["b", "c"],
            ["c", "a"],
            k=60,
        )
        # All three appear in 2 sources each, all should be present
        assert set(result) == {"a", "b", "c"}


class TestSearchGraphForMemories:
    """Tests for _search_graph_for_memories."""

    async def test_returns_direct_memory_ids(self) -> None:
        """_search_graph_for_memories returns memory IDs from entity vector search."""
        llm_service = MagicMock()
        llm_service.get_provider_for_feature = MagicMock(return_value=(AsyncMock(), "haiku", None))

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
                    "entity_key": entity_key(None, "Python"),
                    "name": "Python",
                    "entity_type": "tool",
                    "labels": ["Tool"],
                    "score": 0.9,
                    "memory_ids": ["mem-1", "mem-2"],
                },
                {
                    "entity_key": entity_key(None, "FastAPI"),
                    "name": "FastAPI",
                    "entity_type": "framework",
                    "labels": ["Framework"],
                    "score": 0.8,
                    "memory_ids": ["mem-3"],
                },
            ]
        )
        manager._kg_service.find_related_memory_ids = AsyncMock(return_value=["mem-4"])

        result = await manager._search_graph_for_memories(
            query_embedding=[0.1, 0.2],
            limit=10,
        )

        assert result == ["mem-1", "mem-2", "mem-3", "mem-4"]
        assert manager._kg_service.find_related_memory_ids.await_args.kwargs["entity_keys"] == [
            entity_key(None, "Python"),
            entity_key(None, "FastAPI"),
        ]

    async def test_deduplicates_traversed_ids(self) -> None:
        """_search_graph_for_memories deduplicates IDs from traversal."""
        llm_service = MagicMock()
        llm_service.get_provider_for_feature = MagicMock(return_value=(AsyncMock(), "haiku", None))

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        manager._kg_service.search_entities_by_vector = AsyncMock(
            return_value=[
                {
                    "entity_key": entity_key(None, "A"),
                    "name": "A",
                    "entity_type": "entity",
                    "labels": [],
                    "score": 0.9,
                    "memory_ids": ["mem-1"],
                },
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
        assert result.count("mem-1") == 1

    async def test_returns_empty_when_no_entities(self) -> None:
        """_search_graph_for_memories returns empty when no entity matches."""
        llm_service = MagicMock()
        llm_service.get_provider_for_feature = MagicMock(return_value=(AsyncMock(), "haiku", None))

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
        assert manager._kg_service.search_entities_by_vector.await_args.kwargs[
            "query_embedding"
        ] == [0.1]


class TestSearchMemoriesGraphIntegration:
    """Tests for graph-augmented search_memories."""

    async def test_parallel_search_with_rrf_merge(self) -> None:
        """search_memories runs Qdrant and graph search in parallel, merges via RRF."""
        llm_service = MagicMock()
        llm_service.get_provider_for_feature = MagicMock(return_value=(AsyncMock(), "haiku", None))

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
                {
                    "entity_key": entity_key(None, "A"),
                    "name": "A",
                    "entity_type": "entity",
                    "labels": [],
                    "score": 0.9,
                    "memory_ids": ["mem-2", "mem-3"],
                },
            ]
        )
        manager._kg_service.find_related_memory_ids = AsyncMock(return_value=[])

        # Mock storage
        manager.storage.get_memory = MagicMock(
            side_effect=lambda mid, project_id=None: _mock_memory(mid, f"content of {mid}")
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
        llm_service.get_provider_for_feature = MagicMock(return_value=(AsyncMock(), "haiku", None))

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
            side_effect=lambda mid, project_id=None: _mock_memory(mid, f"content of {mid}")
        )

        result = await manager.search_memories(query="test query", limit=10)

        # Should still return Qdrant results
        assert len(result) == 1
        assert result[0].id == "mem-1"

    async def test_qdrant_only_when_graph_search_disabled(self) -> None:
        """search_memories skips graph search when neo4j_graph_search is False."""
        llm_service = MagicMock()
        llm_service.get_provider_for_feature = MagicMock(return_value=(AsyncMock(), "haiku", None))

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
            side_effect=lambda mid, project_id=None: _mock_memory(mid, f"content of {mid}")
        )

        # Mock the kg_service method to verify it's not called
        if manager._kg_service:
            manager._kg_service.search_entities_by_vector = AsyncMock()

        result = await manager.search_memories(query="test query", limit=10)

        assert len(result) == 1
        # Graph search methods should not have been called
        if manager._kg_service:
            manager._kg_service.search_entities_by_vector.assert_not_called()
            assert manager._kg_service.search_entities_by_vector.call_count == 0
            assert not manager._kg_service.search_entities_by_vector.called

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
            side_effect=lambda mid, project_id=None: _mock_memory(mid, f"content of {mid}")
        )

        result = await manager.search_memories(query="test query", limit=10)

        assert len(result) == 1
        assert result[0].id == "mem-1"

    async def test_user_source_boost_applied(self) -> None:
        """search_memories applies user source boost in graph-augmented mode."""
        llm_service = MagicMock()
        llm_service.get_provider_for_feature = MagicMock(return_value=(AsyncMock(), "haiku", None))

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
        system_mem.source_type = "agent"

        manager.storage.get_memory = MagicMock(
            side_effect=lambda mid, project_id=None: user_mem if mid == "mem-2" else system_mem
        )

        result = await manager.search_memories(query="test", limit=10)

        # Both should be returned
        result_ids = [m.id for m in result]
        assert "mem-1" in result_ids
        assert "mem-2" in result_ids


class TestGraphSearchProjectIdScoping:
    """Tests for project_id scoping in graph-augmented search."""

    async def test_search_graph_for_memories_passes_project_id(self) -> None:
        """_search_graph_for_memories forwards project_id to KG service methods."""
        llm_service = MagicMock()
        llm_service.get_provider_for_feature = MagicMock(return_value=(AsyncMock(), "haiku", None))

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        manager._kg_service.search_entities_by_vector = AsyncMock(
            return_value=[
                {
                    "entity_key": "proj-A::auth",
                    "name": "Auth",
                    "entity_type": "concept",
                    "labels": [],
                    "score": 0.9,
                    "memory_ids": ["mem-1"],
                },
            ]
        )
        manager._kg_service.find_related_memory_ids = AsyncMock(return_value=[])

        await manager._search_graph_for_memories(
            query_embedding=[0.1],
            project_id="proj-A",
        )

        # Verify project_id was passed to both KG methods
        manager._kg_service.search_entities_by_vector.assert_called_once()
        call_kwargs = manager._kg_service.search_entities_by_vector.call_args.kwargs
        assert call_kwargs["project_id"] == "proj-A"

        manager._kg_service.find_related_memory_ids.assert_called_once()
        call_kwargs = manager._kg_service.find_related_memory_ids.call_args.kwargs
        assert call_kwargs["project_id"] == "proj-A"

    async def test_defense_in_depth_skips_cross_project_memories(self) -> None:
        """search_memories skips memories whose project_id doesn't match."""
        llm_service = MagicMock()
        llm_service.get_provider_for_feature = MagicMock(return_value=(AsyncMock(), "haiku", None))

        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
            vector_store=vs,
            embed_fn=embed_fn,
        )

        # Qdrant returns both memories (simulating a leak)
        vs.search = AsyncMock(return_value=[("mem-1", 0.9), ("mem-2", 0.8)])
        manager._kg_service.search_entities_by_vector = AsyncMock(return_value=[])
        manager._kg_service.find_related_memory_ids = AsyncMock(return_value=[])

        # mem-1 belongs to proj-A, mem-2 belongs to proj-B
        mem_a = _mock_memory("mem-1", "content A")
        mem_a.project_id = "proj-A"
        mem_b = _mock_memory("mem-2", "content B")
        mem_b.project_id = "proj-B"

        def _scoped_get_memory(mid: str, project_id: str | None = None):
            mem = mem_a if mid == "mem-1" else mem_b
            if project_id and mem.project_id and mem.project_id != project_id:
                raise ValueError(f"Memory {mid} not found")
            return mem

        def _scoped_get_memories(ids, project_id=None):
            out = []
            for mid in ids:
                try:
                    out.append(_scoped_get_memory(mid, project_id))
                except ValueError:
                    continue
            return out

        manager.storage.get_memory = MagicMock(side_effect=_scoped_get_memory)
        manager.storage.get_memories = MagicMock(side_effect=_scoped_get_memories)

        result = await manager.search_memories(query="test", project_id="proj-A", limit=10)

        result_ids = [m.id for m in result]
        assert "mem-1" in result_ids
        assert "mem-2" not in result_ids  # Cross-project memory filtered out

    async def test_defense_in_depth_allows_null_project_memories(self) -> None:
        """search_memories does NOT skip memories with null project_id (global memories)."""
        llm_service = MagicMock()
        llm_service.get_provider_for_feature = MagicMock(return_value=(AsyncMock(), "haiku", None))

        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
            vector_store=vs,
            embed_fn=embed_fn,
        )

        vs.search = AsyncMock(return_value=[("mem-1", 0.9), ("mem-2", 0.8)])
        manager._kg_service.search_entities_by_vector = AsyncMock(return_value=[])
        manager._kg_service.find_related_memory_ids = AsyncMock(return_value=[])

        mem_a = _mock_memory("mem-1", "content A")
        mem_a.project_id = "proj-A"
        mem_global = _mock_memory("mem-2", "global content")
        mem_global.project_id = None  # Global memory

        manager.storage.get_memory = MagicMock(
            side_effect=lambda mid, project_id=None: mem_a if mid == "mem-1" else mem_global
        )

        result = await manager.search_memories(query="test", project_id="proj-A", limit=10)

        result_ids = [m.id for m in result]
        assert "mem-1" in result_ids
        assert "mem-2" in result_ids  # Global memory NOT filtered


class TestCreateMemoryPassesMemoryId:
    """Tests that create_memory passes memory_id to graph background task."""

    async def test_fire_background_graph_receives_memory_id(self) -> None:
        """_fire_background_graph is called with memory_id from create_memory."""
        llm_service = MagicMock()
        llm_service.get_provider_for_feature = MagicMock(return_value=(AsyncMock(), "haiku", None))

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
        manager.storage.mark_pending_graph = MagicMock()

        await manager.create_memory(content="test content")

        # Graph is now queued via mark_pending_graph, not fired as background task
        manager.storage.mark_pending_graph.assert_called_once_with("test-mem-id")
        assert manager.storage.mark_pending_graph.call_count == 1
        assert manager.storage.mark_pending_graph.call_args is not None


class TestTemporalDecayIntegration:
    """Integration tests for temporal decay in search_memories."""

    @pytest.mark.asyncio
    async def test_graph_enabled_path_keeps_qdrant_similarity(self) -> None:
        """Graph-enabled search should preserve the real Qdrant score for semantic hits."""
        llm_service = MagicMock()
        llm_service.get_provider_for_feature = MagicMock(return_value=(AsyncMock(), "haiku", None))

        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
            vector_store=vs,
            embed_fn=embed_fn,
        )
        object.__setattr__(manager.config, "temporal_decay_half_life_days", 0.0)

        vs.search = AsyncMock(return_value=[("mem-1", 0.675)])
        manager._kg_service.search_entities_by_vector = AsyncMock(return_value=[])
        manager._kg_service.find_related_memory_ids = AsyncMock(return_value=[])
        manager._search_service._keyword_ranked = AsyncMock(return_value=[])

        mem = _mock_memory("mem-1", "content")
        mem.source_type = "agent"
        manager.storage.get_memory = MagicMock(return_value=mem)

        result = await manager.search_memories(query="test", limit=10)

        assert len(result) == 1
        assert result[0].similarity == pytest.approx(0.675)
        assert result[0].ranking_score == pytest.approx(0.675)
        assert result[0].search_via == "semantic"
        assert result[0].ranking_mode == "semantic_only"

    @pytest.mark.asyncio
    async def test_older_memory_ranks_lower_graph_path(self) -> None:
        """In graph-augmented search, an older memory should rank below a recent one."""
        llm_service = MagicMock()
        llm_service.get_provider_for_feature = MagicMock(return_value=(AsyncMock(), "haiku", None))

        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
            vector_store=vs,
            embed_fn=embed_fn,
        )
        # Enable temporal decay with a short half-life for clear differentiation
        object.__setattr__(manager.config, "temporal_decay_half_life_days", 30.0)

        now = datetime.now(UTC)
        recent = now - timedelta(days=1)
        old = now - timedelta(days=90)

        # Both memories rank equally in Qdrant (same position)
        vs.search = AsyncMock(return_value=[("mem-recent", 0.9), ("mem-old", 0.9)])
        manager._kg_service.search_entities_by_vector = AsyncMock(return_value=[])
        manager._kg_service.find_related_memory_ids = AsyncMock(return_value=[])

        mem_recent = _mock_memory("mem-recent", "recent content", updated_at=recent.isoformat())
        mem_old = _mock_memory("mem-old", "old content", updated_at=old.isoformat())

        manager.storage.get_memory = MagicMock(
            side_effect=lambda mid, project_id=None: mem_recent if mid == "mem-recent" else mem_old
        )

        result = await manager.search_memories(query="test", limit=10)

        result_ids = [m.id for m in result]
        assert result_ids[0] == "mem-recent"
        assert result_ids[1] == "mem-old"

    @pytest.mark.asyncio
    async def test_min_score_applies_after_temporal_decay(self) -> None:
        """min_score filters on final semantic similarity after temporal decay."""
        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            vector_store=vs,
            embed_fn=embed_fn,
        )
        object.__setattr__(manager.config, "temporal_decay_half_life_days", 30.0)

        now = datetime.now(UTC)
        old = now - timedelta(days=90)
        vs.search = AsyncMock(return_value=[("mem-old", 0.9)])

        mem_old = _mock_memory("mem-old", "old content", updated_at=old.isoformat())
        mem_old.source_type = "agent"
        manager.storage.get_memory = MagicMock(return_value=mem_old)

        result = await manager.search_memories(query="test", limit=10, min_score=0.3)

        assert result == []
        assert manager.storage.get_memory.call_count == 1

    @pytest.mark.asyncio
    async def test_older_memory_ranks_lower_qdrant_only(self) -> None:
        """In qdrant-only search, an older memory should rank below a recent one."""
        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            vector_store=vs,
            embed_fn=embed_fn,
        )
        object.__setattr__(manager.config, "temporal_decay_half_life_days", 30.0)

        now = datetime.now(UTC)
        recent = now - timedelta(days=1)
        old = now - timedelta(days=90)

        # Same cosine similarity score
        vs.search = AsyncMock(return_value=[("mem-old", 0.9), ("mem-recent", 0.9)])

        mem_recent = _mock_memory("mem-recent", "recent content", updated_at=recent.isoformat())
        mem_old = _mock_memory("mem-old", "old content", updated_at=old.isoformat())

        manager.storage.get_memory = MagicMock(
            side_effect=lambda mid, project_id=None: mem_recent if mid == "mem-recent" else mem_old
        )

        result = await manager.search_memories(query="test", limit=10)

        result_ids = [m.id for m in result]
        assert result_ids[0] == "mem-recent"
        assert result_ids[1] == "mem-old"

    @pytest.mark.asyncio
    async def test_decay_disabled_preserves_order(self) -> None:
        """When half_life=0, temporal decay is disabled and order is unchanged."""
        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            vector_store=vs,
            embed_fn=embed_fn,
        )
        object.__setattr__(manager.config, "temporal_decay_half_life_days", 0.0)

        now = datetime.now(UTC)
        old = now - timedelta(days=365)

        # mem-old comes first from Qdrant with higher score
        vs.search = AsyncMock(return_value=[("mem-old", 0.95), ("mem-recent", 0.8)])

        mem_recent = _mock_memory("mem-recent", "recent", updated_at=now.isoformat())
        mem_old = _mock_memory("mem-old", "old", updated_at=old.isoformat())

        manager.storage.get_memory = MagicMock(
            side_effect=lambda mid, project_id=None: mem_recent if mid == "mem-recent" else mem_old
        )

        result = await manager.search_memories(query="test", limit=10)

        # With decay disabled, original Qdrant ordering is preserved
        result_ids = [m.id for m in result]
        assert result_ids[0] == "mem-old"
        assert result_ids[1] == "mem-recent"
