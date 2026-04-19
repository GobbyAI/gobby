"""Tests for KnowledgeGraphService wiring in MemoryManager."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.persistence import MemoryConfig

pytestmark = pytest.mark.unit


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
        llm_service = MagicMock()
        llm_service.get_default_provider = MagicMock(return_value=AsyncMock())
        embed_fn = AsyncMock(return_value=[0.1, 0.2])
        vs = AsyncMock()

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
            vector_store=vs,
            embed_fn=embed_fn,
        )

        assert manager._kg_service is not None

    def test_kg_service_none_when_no_neo4j(self) -> None:
        """KnowledgeGraphService is None when Neo4j is not configured."""
        llm_service = MagicMock()
        llm_service.get_default_provider = MagicMock(return_value=AsyncMock())

        manager = _make_manager(
            neo4j_url=None,
            llm_service=llm_service,
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
        llm_service = MagicMock()
        llm_service.get_default_provider = MagicMock(return_value=AsyncMock())

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
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
        llm_service = MagicMock()
        llm_service.get_default_provider = MagicMock(return_value=AsyncMock())

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
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


class TestGraphBackgroundTask:
    """Test that create_memory chains a graph background task."""

    async def test_create_memory_fires_graph_task_after_dedup(self) -> None:
        """create_memory fires a graph background task when KnowledgeGraphService is available."""
        llm_service = MagicMock()
        llm_service.get_default_provider = MagicMock(return_value=AsyncMock())

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
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
        llm_service = MagicMock()
        llm_service.get_default_provider = MagicMock(return_value=AsyncMock())

        manager = _make_manager(
            neo4j_url="http://localhost:7474",
            llm_service=llm_service,
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
