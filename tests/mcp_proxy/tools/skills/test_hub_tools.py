"""Tests for hub-related MCP tools: list_hubs and search_hub."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.skills import HubConfig
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations

pytestmark = [pytest.mark.integration]


@pytest.fixture
def db(tmp_path: Path):
    """Create a fresh database with migrations applied."""
    db_path = tmp_path / "test.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    yield database
    database.close()


@pytest.fixture
def mock_hub_manager():
    """Create a mock HubManager with test hubs."""
    manager = MagicMock()

    # Configure list_hubs to return test hub names
    manager.list_hubs.return_value = ["clawdhub", "skillsmp", "my-collection"]

    # Configure has_hub
    def has_hub(name: str) -> bool:
        return name in ["clawdhub", "skillsmp", "my-collection"]

    manager.has_hub.side_effect = has_hub

    # Configure get_config to return test configs
    configs = {
        "clawdhub": HubConfig(type="clawdhub"),
        "skillsmp": HubConfig(type="skillsmp", base_url="https://skillsmp.com/api/v1"),
        "my-collection": HubConfig(type="github-collection"),
    }

    def get_config(name: str) -> HubConfig:
        return configs[name]

    manager.get_config.side_effect = get_config

    return manager


class TestListHubsTool:
    """Tests for list_hubs MCP tool."""

    @pytest.mark.asyncio
    async def test_list_hubs_returns_configured_hubs(self, db, mock_hub_manager):
        """Test that list_hubs returns all configured hubs."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(db, hub_manager=mock_hub_manager)
        tool = registry.get_tool("list_hubs")

        result = await tool()

        assert result["success"] is True
        assert result["count"] == 3
        assert len(result["hubs"]) == 3

    @pytest.mark.asyncio
    async def test_list_hubs_returns_hub_details(self, db, mock_hub_manager):
        """Test that list_hubs returns hub name and type."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(db, hub_manager=mock_hub_manager)
        tool = registry.get_tool("list_hubs")

        result = await tool()

        assert result["success"] is True
        hubs = result["hubs"]

        # Find the clawdhub entry
        clawdhub = next((h for h in hubs if h["name"] == "clawdhub"), None)
        assert clawdhub is not None
        assert clawdhub["type"] == "clawdhub"

        # Find the skillsmp entry
        skillsmp = next((h for h in hubs if h["name"] == "skillsmp"), None)
        assert skillsmp is not None
        assert skillsmp["type"] == "skillsmp"

    @pytest.mark.asyncio
    async def test_list_hubs_without_hub_manager(self, db):
        """Test that list_hubs returns empty list when no hub manager configured."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(db)
        tool = registry.get_tool("list_hubs")

        result = await tool()

        assert result["success"] is True
        assert result["count"] == 0
        assert result["hubs"] == []


class TestSearchHubTool:
    """Tests for search_hub MCP tool."""

    @pytest.mark.asyncio
    async def test_search_hub_requires_query(self, db, mock_hub_manager):
        """Test that search_hub requires a query parameter."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(db, hub_manager=mock_hub_manager)
        tool = registry.get_tool("search_hub")

        # Empty query should fail
        result = await tool(query="")

        assert result["success"] is False
        assert "required" in result["error"].lower() or "empty" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_search_hub_calls_search_all(self, db, mock_hub_manager):
        """Test that search_hub calls hub_manager.search_all."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        # Setup mock search results
        mock_results = [
            {
                "slug": "commit-message",
                "display_name": "Commit Message Generator",
                "description": "Generate conventional commits",
                "hub_name": "clawdhub",
            },
            {
                "slug": "git-helper",
                "display_name": "Git Helper",
                "description": "Git workflow assistant",
                "hub_name": "skillsmp",
            },
        ]
        mock_hub_manager.search_all = AsyncMock(return_value=(mock_results, {}))

        registry = create_skills_registry(db, hub_manager=mock_hub_manager)
        tool = registry.get_tool("search_hub")

        result = await tool(query="commit")

        assert result["success"] is True
        mock_hub_manager.search_all.assert_called_once()
        # Check query was passed
        call_args = mock_hub_manager.search_all.call_args
        assert call_args[1]["query"] == "commit" or call_args[0][0] == "commit"

    @pytest.mark.asyncio
    async def test_search_hub_returns_results(self, db, mock_hub_manager):
        """Test that search_hub returns skill results with slug, name, hub fields."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        mock_results = [
            {
                "slug": "commit-message",
                "display_name": "Commit Message Generator",
                "description": "Generate conventional commits",
                "hub_name": "clawdhub",
            },
        ]
        mock_hub_manager.search_all = AsyncMock(return_value=(mock_results, {}))

        registry = create_skills_registry(db, hub_manager=mock_hub_manager)
        tool = registry.get_tool("search_hub")

        result = await tool(query="commit")

        assert result["success"] is True
        assert result["count"] == 1
        assert len(result["results"]) == 1

        skill = result["results"][0]
        assert skill["slug"] == "commit-message"
        assert skill["display_name"] == "Commit Message Generator"
        assert skill["hub_name"] == "clawdhub"

    @pytest.mark.asyncio
    async def test_search_hub_with_hub_filter(self, db, mock_hub_manager):
        """Test that search_hub can filter by specific hub."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        mock_results = [
            {
                "slug": "commit-message",
                "display_name": "Commit Message Generator",
                "description": "Generate conventional commits",
                "hub_name": "clawdhub",
            },
        ]
        mock_hub_manager.search_all = AsyncMock(return_value=(mock_results, {}))

        registry = create_skills_registry(db, hub_manager=mock_hub_manager)
        tool = registry.get_tool("search_hub")

        result = await tool(query="commit", hub_name="clawdhub")

        assert result["success"] is True
        # Verify hub_names filter was passed
        call_args = mock_hub_manager.search_all.call_args
        assert call_args[1].get("hub_names") == ["clawdhub"]

    @pytest.mark.asyncio
    async def test_search_hub_with_limit(self, db, mock_hub_manager):
        """Test that search_hub respects limit parameter."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        mock_hub_manager.search_all = AsyncMock(return_value=([], {}))

        registry = create_skills_registry(db, hub_manager=mock_hub_manager)
        tool = registry.get_tool("search_hub")

        await tool(query="test", limit=5)

        call_args = mock_hub_manager.search_all.call_args
        assert call_args[1].get("limit") == 5

    @pytest.mark.asyncio
    async def test_search_hub_without_hub_manager(self, db):
        """Test that search_hub returns error when no hub manager configured."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        registry = create_skills_registry(db)
        tool = registry.get_tool("search_hub")

        result = await tool(query="commit")

        assert result["success"] is False
        assert "hub" in result["error"].lower() or "configured" in result["error"].lower()
