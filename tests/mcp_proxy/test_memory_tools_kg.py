"""Tests for knowledge graph MCP tool and memory_stats updates."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.memory.services.knowledge_graph.clustering import ClusterRunResult

pytestmark = pytest.mark.unit


def _make_registry(
    falkordb_host: str | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Create a memory tool registry and return (registry, memory_manager)."""
    from gobby.config.persistence import MemoryConfig
    from gobby.mcp_proxy.tools.memory import create_memory_registry
    from gobby.memory.manager import MemoryManager

    db = MagicMock()
    db.fetchall = MagicMock(return_value=[])
    db.fetchone = MagicMock(return_value=None)
    db.execute = MagicMock()

    config = MemoryConfig()
    manager = MemoryManager(
        db=db,
        config=config,
        falkordb_host=falkordb_host,
        falkordb_password="password" if falkordb_host else None,
    )
    registry = create_memory_registry(lambda: manager)

    return registry, manager


class TestSearchKnowledgeGraphTool:
    """Tests for the search_knowledge_graph MCP tool."""

    def test_search_knowledge_graph_tool_exists(self) -> None:
        """search_knowledge_graph tool is registered in the memory registry."""
        registry, _ = _make_registry()
        tool_names = [t["name"] for t in registry.list_tools()]
        assert "search_knowledge_graph" in tool_names

    @pytest.mark.asyncio
    async def test_search_knowledge_graph_returns_results(self) -> None:
        """search_knowledge_graph returns graph search results."""
        registry, manager = _make_registry()

        # Mock KG service
        from gobby.memory.services.knowledge_graph import KnowledgeGraphService

        kg_service = MagicMock(spec=KnowledgeGraphService)
        kg_service.search_graph = AsyncMock(
            return_value=[
                {"name": "Python", "labels": ["Tool"], "props": {}},
            ]
        )
        manager._kg_service = kg_service

        tool_fn = registry.get_tool("search_knowledge_graph")
        result = await tool_fn(query="programming language", limit=5)

        assert result["success"] is True
        assert len(result["results"]) >= 1
        kg_service.search_graph.assert_called_once_with(
            "programming language",
            limit=5,
            project_id=None,
            include_global=True,
        )

    @pytest.mark.asyncio
    async def test_search_knowledge_graph_returns_empty_when_no_kg_service(self) -> None:
        """search_knowledge_graph returns empty when KG service not available."""
        registry, manager = _make_registry()
        assert manager._kg_service is None

        tool_fn = registry.get_tool("search_knowledge_graph")
        result = await tool_fn(query="test")

        assert result["success"] is True
        assert result["results"] == []


class TestReclusterKnowledgeGraphEntitiesTool:
    """Tests for the recluster_knowledge_graph_entities MCP tool."""

    def test_recluster_knowledge_graph_entities_tool_exists(self) -> None:
        registry, _ = _make_registry()
        tool_names = [t["name"] for t in registry.list_tools()]
        assert "recluster_knowledge_graph_entities" in tool_names

    @pytest.mark.asyncio
    async def test_recluster_knowledge_graph_entities_returns_summary(self) -> None:
        registry, manager = _make_registry()
        kg_service = MagicMock()
        kg_service.recluster_entities = AsyncMock(
            return_value=ClusterRunResult(
                project_id="project-1",
                entity_count=5,
                valid_entity_count=4,
                clustered_entity_count=3,
                noise_count=1,
                invalid_count=1,
                cluster_count=1,
                cluster_ids_by_entity_key={"a": 0, "b": 0, "c": 0, "d": None, "e": None},
                cluster_sizes={0: 3},
                invalid_entity_keys=["e"],
                quality_metrics={"silhouette": None, "clustered_ratio": 0.6},
            )
        )
        manager._kg_service = kg_service

        tool_fn = registry.get_tool("recluster_knowledge_graph_entities")
        result = await tool_fn(project_id="project-1")

        assert result["success"] is True
        assert result["project_id"] == "project-1"
        assert result["clusters"] == [{"cluster_id": 0, "entity_count": 3}]
        assert result["noise_count"] == 1
        assert result["invalid_count"] == 1
        kg_service.recluster_entities.assert_awaited_once_with(project_id="project-1")

    @pytest.mark.asyncio
    async def test_recluster_knowledge_graph_entities_errors_without_kg_service(self) -> None:
        registry, manager = _make_registry()
        assert manager._kg_service is None

        tool_fn = registry.get_tool("recluster_knowledge_graph_entities")
        result = await tool_fn(project_id="project-1")

        assert result["success"] is False
        assert result["error"] == "Knowledge graph service not available"


class TestDensifyKnowledgeGraphCooccurrenceTool:
    """Tests for the densify_knowledge_graph_cooccurrence MCP tool."""

    def test_densify_knowledge_graph_cooccurrence_tool_exists(self) -> None:
        registry, _ = _make_registry()
        tool_names = [t["name"] for t in registry.list_tools()]
        assert "densify_knowledge_graph_cooccurrence" in tool_names

    @pytest.mark.asyncio
    async def test_densify_knowledge_graph_cooccurrence_returns_counters(self) -> None:
        from gobby.memory.services.knowledge_graph.densify import CooccurrenceDensifyResult

        registry, manager = _make_registry()
        kg_service = MagicMock()
        kg_service.densify_cooccurrence = AsyncMock(
            return_value=CooccurrenceDensifyResult(
                project_id="project-1",
                weighted=True,
                memories_scanned=10,
                entities_with_embedding=6,
                pairs_total=12,
                pairs_skipped_no_embedding=2,
                pairs_merged=10,
                batches=1,
                edges_before=0,
                edges_after=10,
            )
        )
        manager._kg_service = kg_service

        tool_fn = registry.get_tool("densify_knowledge_graph_cooccurrence")
        result = await tool_fn(project_id="project-1")

        assert result["success"] is True
        assert result["project_id"] == "project-1"
        assert result["pairs_merged"] == 10
        assert result["pairs_skipped_no_embedding"] == 2
        assert result["edges_after"] == 10
        kg_service.densify_cooccurrence.assert_awaited_once_with(project_id="project-1")

    @pytest.mark.asyncio
    async def test_densify_knowledge_graph_cooccurrence_errors_without_kg_service(self) -> None:
        registry, manager = _make_registry()
        assert manager._kg_service is None

        tool_fn = registry.get_tool("densify_knowledge_graph_cooccurrence")
        result = await tool_fn(project_id="project-1")

        assert result["success"] is False
        assert result["error"] == "Knowledge graph service not available"


class TestExportMemoryGraphRemoved:
    """Test that export_memory_graph tool is removed."""

    def test_export_memory_graph_tool_not_registered(self) -> None:
        """export_memory_graph tool should not exist in registry."""
        registry, _ = _make_registry()
        tool_names = [t["name"] for t in registry.list_tools()]
        assert "export_memory_graph" not in tool_names
