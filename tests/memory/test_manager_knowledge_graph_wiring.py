"""Tests for KnowledgeGraphService wiring in MemoryManager."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.services.knowledge_graph import KnowledgeGraphResult, KnowledgeGraphStatus

pytestmark = pytest.mark.unit


def _mock_llm_service(provider: AsyncMock | None = None) -> MagicMock:
    llm_service = MagicMock()
    llm_service.get_provider_for_feature = MagicMock(
        return_value=(provider or AsyncMock(), "haiku", None)
    )
    return llm_service


def _make_manager(
    neo4j_url: str | None = None,
    llm_service: MagicMock | None = None,
    vector_store: AsyncMock | None = None,
    embed_fn: AsyncMock | None = None,
) -> MagicMock:
    """Create a MemoryManager with controlled dependencies.

    We import lazily so we can patch before construction.
    """
    from gobby.memory.manager import MemoryManager

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
    )


class TestKnowledgeGraphServiceInitialization:
    """Test that KnowledgeGraphService is initialized correctly."""

    def test_kg_service_created_when_neo4j_and_llm_configured(self) -> None:
        """KnowledgeGraphService is created when Neo4j URL + LLM are configured."""
        embed_fn = AsyncMock(return_value=[0.1, 0.2])
        vs = AsyncMock()

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=_mock_llm_service(),
            vector_store=vs,
            embed_fn=embed_fn,
        )

        assert manager._kg_service is not None

    def test_kg_service_uses_configured_provider_and_model(self) -> None:
        """KnowledgeGraphService wiring should honor memory.kg provider/model."""
        provider = AsyncMock()
        llm_service = _mock_llm_service(provider)

        with patch("gobby.memory.manager.KnowledgeGraphService") as mock_kg_service:
            manager = _make_manager(
                neo4j_url="http://localhost:7474",
                llm_service=llm_service,
                vector_store=AsyncMock(),
                embed_fn=AsyncMock(return_value=[0.1]),
            )

        llm_service.get_provider_for_feature.assert_called_once_with(manager.config.kg)
        call_kwargs = mock_kg_service.call_args.kwargs
        assert call_kwargs["llm_provider"] is provider
        assert call_kwargs["model"] == "haiku"

    def test_kg_service_none_when_no_neo4j(self) -> None:
        """KnowledgeGraphService is None when Neo4j is not configured."""
        manager = _make_manager(
            neo4j_url=None,
            llm_service=_mock_llm_service(),
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(),
        )

        assert manager._kg_service is None

    def test_kg_service_none_when_no_llm(self) -> None:
        """KnowledgeGraphService is None when LLM service not available."""
        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=None,
        )

        assert manager._kg_service is None


class TestGraphDelegation:
    """Test that graph read methods delegate to KnowledgeGraphService."""

    async def test_get_entity_graph_delegates_to_kg_service(self) -> None:
        """get_entity_graph delegates to KnowledgeGraphService."""
        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=_mock_llm_service(),
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        expected = {"entities": [{"name": "Josh"}], "relationships": []}
        manager._kg_service.get_entity_graph = AsyncMock(return_value=expected)

        result = await manager.get_entity_graph(limit=100)

        assert result == expected
        manager._kg_service.get_entity_graph.assert_called_once_with(limit=100, project_id=None)

    async def test_get_entity_neighbors_delegates_to_kg_service(self) -> None:
        """get_entity_neighbors delegates to KnowledgeGraphService."""
        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=_mock_llm_service(),
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        expected = {"entities": [], "relationships": []}
        manager._kg_service.get_entity_neighbors = AsyncMock(return_value=expected)

        result = await manager.get_entity_neighbors("Josh")

        assert result == expected
        manager._kg_service.get_entity_neighbors.assert_called_once_with(
            "Josh",
            project_id=None,
        )

    async def test_get_entity_graph_returns_none_when_no_kg_service(self) -> None:
        """get_entity_graph returns None when KnowledgeGraphService is not available."""
        manager = _make_manager(neo4j_url=None)

        result = await manager.get_entity_graph()

        assert result is None

    async def test_get_entity_neighbors_returns_none_when_no_kg_service(self) -> None:
        """get_entity_neighbors returns None when KnowledgeGraphService is not available."""
        manager = _make_manager(neo4j_url=None)

        result = await manager.get_entity_neighbors("Josh")

        assert result is None

    async def test_clear_knowledge_graph_requeues_affected_memories(self) -> None:
        """clear_knowledge_graph should reset graph_processed for affected memories."""
        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=_mock_llm_service(),
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        manager._kg_service.clear_graph = AsyncMock(
            return_value={"memories_deleted": 2, "entities_deleted": 4}
        )
        manager.storage.mark_pending_graphs = MagicMock(return_value=3)

        result = await manager.clear_knowledge_graph(project_id="proj-1")

        manager._kg_service.clear_graph.assert_awaited_once_with(project_id="proj-1")
        manager.storage.mark_pending_graphs.assert_called_once_with("proj-1")
        assert result == {
            "success": True,
            "memories_marked_pending": 3,
            "memories_deleted": 2,
            "entities_deleted": 4,
        }

    async def test_rebuild_knowledge_graph_marks_successful_memories_processed(self) -> None:
        """Explicit rebuild should reconcile graph_processed for successful rows."""
        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=_mock_llm_service(),
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        mem1 = MagicMock(id="mem-1", content="First memory", project_id="proj-1")
        mem2 = MagicMock(id="mem-2", content="Second memory", project_id="proj-1")
        mem3 = MagicMock(id="mem-3", content="Third memory", project_id="proj-1")

        manager._fetch_all_project_memories = AsyncMock(return_value=[mem1, mem2, mem3])
        manager._kg_service.add_to_graph = AsyncMock(
            side_effect=[
                KnowledgeGraphResult(KnowledgeGraphStatus.SUCCESS),
                KnowledgeGraphResult(KnowledgeGraphStatus.NOOP_NO_ENTITIES),
                KnowledgeGraphResult(KnowledgeGraphStatus.DETERMINISTIC_FAILURE),
            ]
        )
        manager.mark_graph_processed = MagicMock()

        result = await manager.rebuild_knowledge_graph(project_id="proj-1")

        assert manager._kg_service.add_to_graph.await_count == 3
        manager.mark_graph_processed.assert_any_call("mem-1")
        manager.mark_graph_processed.assert_any_call("mem-2")
        assert manager.mark_graph_processed.call_count == 2
        assert result["memories_processed"] == 3
        assert result["memories_marked_processed"] == 2
        assert result["memories_extracted"] == 1
        assert result["noop_no_entities"] == 1
        assert result["errors"] == 1


class TestGraphBackgroundTask:
    """Test that create_memory chains a graph background task."""

    async def test_create_memory_fires_graph_task_after_dedup(self) -> None:
        """create_memory fires a graph background task when KnowledgeGraphService is available."""
        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=_mock_llm_service(),
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        # Mock the backend to avoid real DB operations
        manager._backend = AsyncMock()
        manager._backend.content_exists = AsyncMock(return_value=False)

        from gobby.memory.protocol import MemoryRecord

        mock_record = MagicMock(spec=MemoryRecord)
        mock_record.id = "test-id"
        mock_record.memory_type = "fact"
        mock_record.content = "Josh uses Python"
        mock_record.created_at = MagicMock(isoformat=MagicMock(return_value="2026-01-01"))
        mock_record.updated_at = MagicMock(isoformat=MagicMock(return_value="2026-01-01"))
        mock_record.project_id = None
        mock_record.source_type = "user"
        mock_record.source_session_id = None

        mock_record.access_count = 0
        mock_record.last_accessed_at = None
        mock_record.tags = []
        manager._backend.create = AsyncMock(return_value=mock_record)

        # Mock KG service
        manager._kg_service.add_to_graph = AsyncMock()

        manager.storage.mark_pending_graph = MagicMock()

        await manager.create_memory(content="Josh uses Python")

        # Graph is now queued via mark_pending_graph, not fired as background task
        manager.storage.mark_pending_graph.assert_called_once()

    async def test_create_memory_no_graph_task_when_no_kg_service(self) -> None:
        """create_memory doesn't fire graph task when KnowledgeGraphService is unavailable."""
        manager = _make_manager(neo4j_url=None)

        manager._backend = AsyncMock()
        manager._backend.content_exists = AsyncMock(return_value=False)

        from gobby.memory.protocol import MemoryRecord

        mock_record = MagicMock(spec=MemoryRecord)
        mock_record.id = "test-id"
        mock_record.memory_type = "fact"
        mock_record.content = "test"
        mock_record.created_at = MagicMock(isoformat=MagicMock(return_value="2026-01-01"))
        mock_record.updated_at = MagicMock(isoformat=MagicMock(return_value="2026-01-01"))
        mock_record.project_id = None
        mock_record.source_type = "user"
        mock_record.source_session_id = None

        mock_record.access_count = 0
        mock_record.last_accessed_at = None
        mock_record.tags = []
        manager._backend.create = AsyncMock(return_value=mock_record)

        await manager.create_memory(content="test")

        # No graph background tasks should exist
        graph_tasks = [t for t in manager._background_tasks if "graph" in (t.get_name() or "")]
        assert len(graph_tasks) == 0

    async def test_graph_task_failure_logged_not_raised(self) -> None:
        """Graph background task failure is logged but doesn't propagate."""
        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=_mock_llm_service(),
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        manager._backend = AsyncMock()
        manager._backend.content_exists = AsyncMock(return_value=False)

        from gobby.memory.protocol import MemoryRecord

        mock_record = MagicMock(spec=MemoryRecord)
        mock_record.id = "test-id"
        mock_record.memory_type = "fact"
        mock_record.content = "test"
        mock_record.created_at = MagicMock(isoformat=MagicMock(return_value="2026-01-01"))
        mock_record.updated_at = MagicMock(isoformat=MagicMock(return_value="2026-01-01"))
        mock_record.project_id = None
        mock_record.source_type = "user"
        mock_record.source_session_id = None

        mock_record.access_count = 0
        mock_record.last_accessed_at = None
        mock_record.tags = []
        manager._backend.create = AsyncMock(return_value=mock_record)

        # Make graph service fail
        manager._kg_service.add_to_graph = AsyncMock(side_effect=Exception("Neo4j down"))

        # Should not raise
        await manager.create_memory(content="test")
        await asyncio.sleep(0.1)


class TestNoGraphServiceReference:
    """Test that old GraphService is no longer referenced."""

    def test_manager_has_no_graph_service_attribute(self) -> None:
        """MemoryManager should not have _graph_service attribute (replaced by _kg_service)."""
        manager = _make_manager()
        assert not hasattr(manager, "_graph_service")
