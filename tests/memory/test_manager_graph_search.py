"""Tests for MemoryManager graph-augmented search: parallel search, RRF merge, degradation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.identity import entity_key
from gobby.memory.manager import MemoryManager
from gobby.memory.services.knowledge_graph.reader import RelatedMemoryTraversal
from gobby.memory.write_result import MemoryWriteResult
from gobby.storage.memories_models import MemoryType
from gobby.storage.memories_scope import MemoryScope, memory_matches_scope
from gobby.storage.projects import GLOBAL_PROJECT_ID, PERSONAL_PROJECT_ID

pytestmark = pytest.mark.unit


def _make_manager(
    falkordb_host: str | None = None,
    llm_service: MagicMock | None = None,
    vector_store: AsyncMock | None = None,
    embed_fn: AsyncMock | None = None,
    graph_search: bool = True,
    graph_min_score: float = 0.5,
    rrf_k: int = 60,
    config: MemoryConfig | None = None,
) -> MemoryManager:
    """Create a MemoryManager with controlled dependencies."""
    db = MagicMock()
    db.fetchall = MagicMock(return_value=[])
    db.fetchone = MagicMock(return_value=None)
    db.execute = MagicMock()

    config = config if config is not None else MemoryConfig()

    kwargs: dict[str, Any] = {
        "db": db,
        "config": config,
        "llm_service": llm_service,
        "vector_store": vector_store,
        "embed_fn": embed_fn,
        "falkordb_host": falkordb_host,
        "falkordb_password": "secret" if falkordb_host else None,
        "falkordb_graph_search": graph_search,
        "falkordb_graph_min_score": graph_min_score,
        "falkordb_rrf_k": rrf_k,
    }
    if falkordb_host:
        with patch("gobby.memory.manager.FalkorClient") as falkor_cls:
            falkor_cls.return_value = AsyncMock()
            return MemoryManager(**kwargs)
    return MemoryManager(**kwargs)


def _mock_llm_service() -> MagicMock:
    llm_service = MagicMock()
    llm_service.call_json_feature = AsyncMock(return_value={"entities": [], "relations": []})
    return llm_service


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
    m.project_id = PERSONAL_PROJECT_ID
    m.is_global = False
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
        """RRF with three sources (Qdrant + graph + keyword)."""
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
        llm_service = _mock_llm_service()

        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=llm_service,
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        # Mock entity search results
        manager._kg_service.search_entities_by_vector = AsyncMock(
            return_value=[
                {
                    "entity_key": entity_key(GLOBAL_PROJECT_ID, "Python", is_global=True),
                    "name": "Python",
                    "entity_type": "tool",
                    "labels": ["Tool"],
                    "score": 0.9,
                    "memory_ids": ["mem-1", "mem-2"],
                },
                {
                    "entity_key": entity_key(GLOBAL_PROJECT_ID, "FastAPI", is_global=True),
                    "name": "FastAPI",
                    "entity_type": "framework",
                    "labels": ["Framework"],
                    "score": 0.8,
                    "memory_ids": ["mem-3"],
                },
            ]
        )
        manager._kg_service.find_related_memory_ids = AsyncMock(
            return_value=RelatedMemoryTraversal(memory_ids=["mem-4"])
        )

        result = await manager._search_graph_for_memories(
            query_embedding=[0.1, 0.2],
            limit=10,
        )

        assert result == ["mem-1", "mem-2", "mem-3", "mem-4"]
        assert manager._kg_service.find_related_memory_ids.await_args.kwargs["entity_keys"] == [
            entity_key(GLOBAL_PROJECT_ID, "Python", is_global=True),
            entity_key(GLOBAL_PROJECT_ID, "FastAPI", is_global=True),
        ]
        assert manager._kg_service.find_related_memory_ids.await_args.kwargs["max_hops"] == 1

    async def test_caps_expansion_entity_seeds_but_keeps_direct_ids(self) -> None:
        """_search_graph_for_memories caps expansion seeds without dropping direct hits."""
        llm_service = _mock_llm_service()

        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=llm_service,
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        entity_results = [
            {
                "entity_key": entity_key(GLOBAL_PROJECT_ID, f"Entity{i}", is_global=True),
                "name": f"Entity{i}",
                "entity_type": "entity",
                "labels": [],
                "score": 0.9,
                "memory_ids": [f"mem-{i}"],
            }
            for i in range(10)
        ]
        manager._kg_service.search_entities_by_vector = AsyncMock(return_value=entity_results)
        manager._kg_service.find_related_memory_ids = AsyncMock(
            return_value=RelatedMemoryTraversal(memory_ids=["mem-related"])
        )

        result = await manager._search_graph_for_memories(
            query_embedding=[0.1],
            limit=20,
        )

        assert result == [*(f"mem-{i}" for i in range(10)), "mem-related"]
        assert manager._kg_service.find_related_memory_ids.await_args.kwargs["entity_keys"] == [
            entity_key(GLOBAL_PROJECT_ID, f"Entity{i}", is_global=True) for i in range(8)
        ]

    @pytest.mark.parametrize("error", [TimeoutError("slow traversal"), RuntimeError("boom")])
    async def test_returns_direct_ids_when_related_expansion_fails(
        self,
        error: Exception,
    ) -> None:
        """_search_graph_for_memories keeps direct IDs when traversal fails."""
        llm_service = _mock_llm_service()

        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=llm_service,
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        manager._kg_service.search_entities_by_vector = AsyncMock(
            return_value=[
                {
                    "entity_key": entity_key(GLOBAL_PROJECT_ID, "Python", is_global=True),
                    "name": "Python",
                    "entity_type": "tool",
                    "labels": [],
                    "score": 0.9,
                    "memory_ids": ["mem-1", "mem-2"],
                }
            ]
        )
        manager._kg_service.find_related_memory_ids = AsyncMock(side_effect=error)

        result = await manager._search_graph_for_memories(
            query_embedding=[0.1],
            limit=10,
        )

        assert result == ["mem-1", "mem-2"]
        manager._kg_service.search_entities_by_vector.assert_awaited_once()
        manager._kg_service.find_related_memory_ids.assert_awaited_once()
        assert manager._kg_service.find_related_memory_ids.await_args.kwargs["entity_keys"] == [
            entity_key(GLOBAL_PROJECT_ID, "Python", is_global=True)
        ]
        assert manager._kg_service.find_related_memory_ids.await_args.kwargs["max_hops"] == 1

    async def test_deduplicates_traversed_ids(self) -> None:
        """_search_graph_for_memories deduplicates IDs from traversal."""
        llm_service = _mock_llm_service()

        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=llm_service,
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        manager._kg_service.search_entities_by_vector = AsyncMock(
            return_value=[
                {
                    "entity_key": entity_key(GLOBAL_PROJECT_ID, "A", is_global=True),
                    "name": "A",
                    "entity_type": "entity",
                    "labels": [],
                    "score": 0.9,
                    "memory_ids": ["mem-1"],
                },
            ]
        )
        # Traversal returns overlapping ID
        manager._kg_service.find_related_memory_ids = AsyncMock(
            return_value=RelatedMemoryTraversal(memory_ids=["mem-1", "mem-2"])
        )

        result = await manager._search_graph_for_memories(
            query_embedding=[0.1],
            limit=10,
        )

        # mem-1 should appear only once
        assert result == ["mem-1", "mem-2"]
        assert result.count("mem-1") == 1

    async def test_returns_empty_when_no_entities(self) -> None:
        """_search_graph_for_memories returns empty when no entity matches."""
        llm_service = _mock_llm_service()

        manager = _make_manager(
            falkordb_host="127.0.0.1",
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
        llm_service = _mock_llm_service()

        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1, 0.2])

        manager = _make_manager(
            falkordb_host="127.0.0.1",
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
                    "entity_key": entity_key(GLOBAL_PROJECT_ID, "A", is_global=True),
                    "name": "A",
                    "entity_type": "entity",
                    "labels": [],
                    "score": 0.9,
                    "memory_ids": ["mem-2", "mem-3"],
                },
            ]
        )
        manager._kg_service.find_related_memory_ids = AsyncMock(
            return_value=RelatedMemoryTraversal()
        )

        # Mock storage
        cast(Any, manager.storage).get_memory = MagicMock(
            side_effect=lambda mid, scope=None: _mock_memory(mid, f"content of {mid}")
        )

        result = await manager.search_memories(query="test query", limit=10)

        assert len(result) >= 2
        result_ids = [m.id for m in result]
        # mem-2 appears in both sources, should rank high
        assert "mem-2" in result_ids
        assert "mem-1" in result_ids

    async def test_graceful_degradation_graph_failure(self) -> None:
        """search_memories falls back to Qdrant-only when graph search fails."""
        llm_service = _mock_llm_service()

        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=llm_service,
            vector_store=vs,
            embed_fn=embed_fn,
        )

        # Qdrant works
        vs.search = AsyncMock(return_value=[("mem-1", 0.9)])

        # Graph search fails
        manager._kg_service.search_entities_by_vector = AsyncMock(
            side_effect=Exception("FalkorDB down")
        )

        cast(Any, manager.storage).get_memory = MagicMock(
            side_effect=lambda mid, scope=None: _mock_memory(mid, f"content of {mid}")
        )

        result = await manager.search_memories(query="test query", limit=10)

        # Should still return Qdrant results
        assert len(result) == 1
        assert result[0].id == "mem-1"

    async def test_qdrant_only_when_graph_search_disabled(self) -> None:
        """search_memories skips graph search when falkordb_graph_search is False."""
        llm_service = _mock_llm_service()

        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=llm_service,
            vector_store=vs,
            embed_fn=embed_fn,
            graph_search=False,
        )

        vs.search = AsyncMock(return_value=[("mem-1", 0.9)])
        cast(Any, manager.storage).get_memory = MagicMock(
            side_effect=lambda mid, scope=None: _mock_memory(mid, f"content of {mid}")
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
            falkordb_host=None,  # No FalkorDB
            vector_store=vs,
            embed_fn=embed_fn,
        )

        vs.search = AsyncMock(return_value=[("mem-1", 0.8)])
        cast(Any, manager.storage).get_memory = MagicMock(
            side_effect=lambda mid, scope=None: _mock_memory(mid, f"content of {mid}")
        )

        result = await manager.search_memories(query="test query", limit=10)

        assert len(result) == 1
        assert result[0].id == "mem-1"

    async def test_user_source_boost_applied(self) -> None:
        """search_memories applies user source boost in graph-augmented mode."""
        llm_service = _mock_llm_service()

        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=llm_service,
            vector_store=vs,
            embed_fn=embed_fn,
        )

        # Both memories appear in Qdrant, but mem-2 is user-sourced
        vs.search = AsyncMock(return_value=[("mem-1", 0.9), ("mem-2", 0.85)])
        manager._kg_service.search_entities_by_vector = AsyncMock(return_value=[])
        manager._kg_service.find_related_memory_ids = AsyncMock(
            return_value=RelatedMemoryTraversal()
        )

        user_mem = _mock_memory("mem-2", "user content")
        user_mem.source_type = "user"
        system_mem = _mock_memory("mem-1", "system content")
        system_mem.source_type = "agent"

        cast(Any, manager.storage).get_memory = MagicMock(
            side_effect=lambda mid, scope=None: user_mem if mid == "mem-2" else system_mem
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
        llm_service = _mock_llm_service()

        manager = _make_manager(
            falkordb_host="127.0.0.1",
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
        manager._kg_service.find_related_memory_ids = AsyncMock(
            return_value=RelatedMemoryTraversal()
        )

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
        assert call_kwargs["max_hops"] == 1

    async def test_defense_in_depth_skips_cross_project_memories(self) -> None:
        """search_memories skips memories whose project_id doesn't match."""
        llm_service = _mock_llm_service()

        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=llm_service,
            vector_store=vs,
            embed_fn=embed_fn,
        )

        # Qdrant returns both memories (simulating a leak)
        vs.search = AsyncMock(return_value=[("mem-1", 0.9), ("mem-2", 0.8)])
        manager._kg_service.search_entities_by_vector = AsyncMock(return_value=[])
        manager._kg_service.find_related_memory_ids = AsyncMock(
            return_value=RelatedMemoryTraversal()
        )

        # mem-1 belongs to proj-A, mem-2 belongs to proj-B
        mem_a = _mock_memory("mem-1", "content A")
        mem_a.project_id = "proj-A"
        mem_b = _mock_memory("mem-2", "content B")
        mem_b.project_id = "proj-B"

        def _scoped_get_memory(mid: str, scope: MemoryScope | None = None):
            mem = mem_a if mid == "mem-1" else mem_b
            if scope is not None and not memory_matches_scope(mem.project_id, mem.is_global, scope):
                raise ValueError(f"Memory {mid} not found")
            return mem

        def _scoped_get_memories(ids, scope: MemoryScope | None = None):
            out = []
            for mid in ids:
                try:
                    out.append(_scoped_get_memory(mid, scope))
                except ValueError:
                    continue
            return out

        cast(Any, manager.storage).get_memory = MagicMock(side_effect=_scoped_get_memory)
        manager.storage.get_memories = MagicMock(side_effect=_scoped_get_memories)

        result = await manager.search_memories(query="test", project_id="proj-A", limit=10)

        result_ids = [m.id for m in result]
        assert "mem-1" in result_ids
        assert "mem-2" not in result_ids  # Cross-project memory filtered out

    async def test_defense_in_depth_allows_explicit_global_memories(self) -> None:
        """Project-visible search retains explicitly global memories."""
        llm_service = _mock_llm_service()

        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=llm_service,
            vector_store=vs,
            embed_fn=embed_fn,
        )

        vs.search = AsyncMock(return_value=[("mem-1", 0.9), ("mem-2", 0.8)])
        manager._kg_service.search_entities_by_vector = AsyncMock(return_value=[])
        manager._kg_service.find_related_memory_ids = AsyncMock(
            return_value=RelatedMemoryTraversal()
        )

        mem_a = _mock_memory("mem-1", "content A")
        mem_a.project_id = "proj-A"
        mem_global = _mock_memory("mem-2", "global content")
        mem_global.project_id = PERSONAL_PROJECT_ID
        mem_global.is_global = True

        cast(Any, manager.storage).get_memory = MagicMock(
            side_effect=lambda mid, scope=None: mem_a if mid == "mem-1" else mem_global
        )

        result = await manager.search_memories(query="test", project_id="proj-A", limit=10)

        result_ids = [m.id for m in result]
        assert "mem-1" in result_ids
        assert "mem-2" in result_ids  # Global memory NOT filtered


class TestCreateMemoryPassesMemoryId:
    """Tests that create_memory passes memory_id to graph background task."""

    async def test_fire_background_graph_receives_memory_id(self) -> None:
        """_fire_background_graph is called with memory_id from create_memory."""
        llm_service = _mock_llm_service()

        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=llm_service,
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        # Mock the backend
        manager._backend = AsyncMock()
        manager._backend.content_exists = AsyncMock(return_value=False)

        from gobby.memory.protocol import MemoryRecord

        mock_record = MemoryRecord(
            id="test-mem-id",
            memory_type=MemoryType.FACT,
            content="test content",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            project_id=PERSONAL_PROJECT_ID,
            source_type="user",
            source_session_id=None,
            access_count=0,
            last_accessed_at=None,
            tags=[],
        )
        manager._backend.create = AsyncMock(return_value=MemoryWriteResult(mock_record, "created"))

        manager._kg_service.add_to_graph = AsyncMock()
        manager._lifecycle_service._reconcile_active_snapshot = AsyncMock(return_value=True)

        created = await manager.create_memory(content="test content")

        # Graph queuing now happens inside the active-row reconciliation fence.
        assert created.id == "test-mem-id"
        assert created.content == "test content"
        assert created.project_id == PERSONAL_PROJECT_ID
        manager._lifecycle_service._reconcile_active_snapshot.assert_awaited_once()


class TestTemporalDecayIntegration:
    """Integration tests for temporal decay in search_memories."""

    @pytest.mark.asyncio
    async def test_graph_enabled_path_keeps_qdrant_similarity(self) -> None:
        """Graph-enabled search should preserve the real Qdrant score for semantic hits."""
        llm_service = _mock_llm_service()

        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=llm_service,
            vector_store=vs,
            embed_fn=embed_fn,
        )
        object.__setattr__(manager.config, "temporal_decay_half_life_days", 0.0)

        vs.search = AsyncMock(return_value=[("mem-1", 0.675)])
        manager._kg_service.search_entities_by_vector = AsyncMock(return_value=[])
        manager._kg_service.find_related_memory_ids = AsyncMock(
            return_value=RelatedMemoryTraversal()
        )
        manager._search_service._keyword_ranked = AsyncMock(return_value=[])

        mem = _mock_memory("mem-1", "content")
        mem.source_type = "agent"
        cast(Any, manager.storage).get_memory = MagicMock(return_value=mem)

        result = await manager.search_memories(query="test", limit=10)

        assert len(result) == 1
        assert result[0].similarity == pytest.approx(0.675)
        assert result[0].ranking_score == pytest.approx(0.675)
        assert result[0].search_via == "semantic"
        assert result[0].ranking_mode == "semantic_only"

    @pytest.mark.asyncio
    async def test_older_memory_ranks_lower_graph_path(self) -> None:
        """In graph-augmented search, an older memory should rank below a recent one."""
        llm_service = _mock_llm_service()

        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            falkordb_host="127.0.0.1",
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
        manager._kg_service.find_related_memory_ids = AsyncMock(
            return_value=RelatedMemoryTraversal()
        )

        mem_recent = _mock_memory("mem-recent", "recent content", updated_at=recent.isoformat())
        mem_old = _mock_memory("mem-old", "old content", updated_at=old.isoformat())

        cast(Any, manager.storage).get_memory = MagicMock(
            side_effect=lambda mid, scope=None: mem_recent if mid == "mem-recent" else mem_old
        )

        result = await manager.search_memories(query="test", limit=10)

        result_ids = [m.id for m in result]
        assert result_ids[0] == "mem-recent"
        assert result_ids[1] == "mem-old"

    @pytest.mark.asyncio
    async def test_min_score_filters_on_relevance_not_age(self) -> None:
        """min_score gates the undecayed score; decay only orders what survives.

        This asserted the opposite until #20858. Filtering the decayed similarity
        made the floor unsatisfiable as memories aged -- at the live corpus median
        age of 25.9 days the decay factor is 0.549, so the 0.55 search floor
        demanded a cosine of 1.002 -- and the slots it emptied were taken by
        keyword-only hits carrying no cosine at all.
        """
        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(
            vector_store=vs,
            embed_fn=embed_fn,
        )
        object.__setattr__(manager.config, "temporal_decay_half_life_days", 30.0)

        now = datetime.now(UTC)
        old = now - timedelta(days=90)
        # Three half-lives old: decay 0.125, so the decayed score is 0.1125.
        vs.search = AsyncMock(return_value=[("mem-old", 0.9)])

        mem_old = _mock_memory("mem-old", "old content", updated_at=old.isoformat())
        mem_old.source_type = "agent"
        cast(Any, manager.storage).get_memory = MagicMock(return_value=mem_old)

        result = await manager.search_memories(query="test", limit=10, min_score=0.3)

        # Admitted on its 0.9 cosine, and it still carries the decayed score for
        # ranking -- age keeps ordering results, it just no longer evicts them.
        assert [mem.id for mem in result] == ["mem-old"]
        assert result[0].similarity == pytest.approx(0.1125)
        assert result[0].raw_semantic_score == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_min_score_still_drops_a_fresh_weak_match(self) -> None:
        """The corrected axis is not a blanket loosening: weak is still weak.

        Undecayed >= decayed always, so moving the axis can only ever admit more at
        a fixed value. What proves it is still a relevance test is that the SAME
        undecayed memory falls on either side of the floor as the floor crosses its
        cosine -- age never enters, at either setting.
        """
        vs = AsyncMock()
        embed_fn = AsyncMock(return_value=[0.1])

        manager = _make_manager(vector_store=vs, embed_fn=embed_fn)
        object.__setattr__(manager.config, "temporal_decay_half_life_days", 30.0)

        now = datetime.now(UTC)
        vs.search = AsyncMock(return_value=[("mem-fresh", 0.2)])

        mem_fresh = _mock_memory("mem-fresh", "fresh content", updated_at=now.isoformat())
        mem_fresh.source_type = "agent"
        cast(Any, manager.storage).get_memory = MagicMock(return_value=mem_fresh)

        assert await manager.search_memories(query="test", limit=10, min_score=0.3) == []

        admitted = await manager.search_memories(query="test", limit=10, min_score=0.15)

        assert [mem.id for mem in admitted] == ["mem-fresh"]
        assert admitted[0].raw_semantic_score == pytest.approx(0.2)
        # Fresh, so decay is ~1.0 and the two axes agree here by construction.
        assert admitted[0].similarity == pytest.approx(0.2, abs=1e-3)

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

        cast(Any, manager.storage).get_memory = MagicMock(
            side_effect=lambda mid, scope=None: mem_recent if mid == "mem-recent" else mem_old
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

        # Half-life resolves once at construction (#17200), so disable decay
        # via config up front rather than mutating the built manager.
        manager = _make_manager(
            vector_store=vs,
            embed_fn=embed_fn,
            config=MemoryConfig(temporal_decay_half_life_days=0.0),
        )

        now = datetime.now(UTC)
        old = now - timedelta(days=365)

        # mem-old comes first from Qdrant with higher score
        vs.search = AsyncMock(return_value=[("mem-old", 0.95), ("mem-recent", 0.8)])

        mem_recent = _mock_memory("mem-recent", "recent", updated_at=now.isoformat())
        mem_old = _mock_memory("mem-old", "old", updated_at=old.isoformat())

        cast(Any, manager.storage).get_memory = MagicMock(
            side_effect=lambda mid, scope=None: mem_recent if mid == "mem-recent" else mem_old
        )

        result = await manager.search_memories(query="test", limit=10)

        # With decay disabled, original Qdrant ordering is preserved
        result_ids = [m.id for m in result]
        assert result_ids[0] == "mem-old"
        assert result_ids[1] == "mem-recent"


@pytest.mark.asyncio
async def test_configured_expansion_deadline_preserves_direct_memory_ids() -> None:
    import asyncio

    timeout_seconds = 0.001
    manager = _make_manager(
        falkordb_host="127.0.0.1",
        llm_service=_mock_llm_service(),
        vector_store=AsyncMock(),
        embed_fn=AsyncMock(return_value=[0.1]),
        config=MemoryConfig(
            graph_related_expansion_timeout_seconds=timeout_seconds,
        ),
    )
    kg_service = manager._kg_service
    assert kg_service is not None
    entity_search = AsyncMock(
        return_value=[
            {
                "entity_key": entity_key(GLOBAL_PROJECT_ID, "Python", is_global=True),
                "name": "Python",
                "entity_type": "tool",
                "labels": [],
                "score": 0.9,
                "memory_ids": ["mem-direct"],
            }
        ]
    )

    async def blocked_query(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        await asyncio.Event().wait()
        return []

    with (
        patch.object(kg_service, "search_entities_by_vector", new=entity_search),
        patch.object(
            kg_service._reader._falkor,
            "query",
            new=AsyncMock(side_effect=blocked_query),
        ),
    ):
        result = await manager._search_graph_for_memories(
            query_embedding=[0.1],
            limit=10,
        )

    assert result == ["mem-direct"]
    assert kg_service._reader._traversal_timeout_count == 1
