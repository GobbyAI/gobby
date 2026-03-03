"""Tests for knowledge graph MCP tool and memory_stats updates."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit


def _make_registry(
    neo4j_url: str | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Create a memory tool registry and return (registry, memory_manager)."""
    from gobby.config.persistence import MemoryConfig
    from gobby.mcp_proxy.tools.memory import create_memory_registry
    from gobby.memory.manager import MemoryManager

    db = MagicMock()
    db.fetchall = MagicMock(return_value=[])
    db.fetchone = MagicMock(return_value=None)
    db.execute = MagicMock()

    config = MemoryConfig(
        neo4j_url=neo4j_url,
        neo4j_auth="neo4j:password" if neo4j_url else None,
    )

    manager = MemoryManager(db=db, config=config)
    registry = create_memory_registry(manager)

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
        registry, manager = _make_registry(neo4j_url="http://localhost:7474")

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
        kg_service.search_graph.assert_called_once_with("programming language", limit=5)

    @pytest.mark.asyncio
    async def test_search_knowledge_graph_returns_empty_when_no_kg_service(self) -> None:
        """search_knowledge_graph returns empty when KG service not available."""
        registry, manager = _make_registry()
        assert manager._kg_service is None

        tool_fn = registry.get_tool("search_knowledge_graph")
        result = await tool_fn(query="test")

        assert result["success"] is True
        assert result["results"] == []


class TestExportMemoryGraphRemoved:
    """Test that export_memory_graph tool is removed."""

    def test_export_memory_graph_tool_not_registered(self) -> None:
        """export_memory_graph tool should not exist in registry."""
        registry, _ = _make_registry()
        tool_names = [t["name"] for t in registry.list_tools()]
        assert "export_memory_graph" not in tool_names
