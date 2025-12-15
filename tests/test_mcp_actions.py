"""Tests for src/mcp_proxy/actions.py - MCP Actions."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from gobby.mcp_proxy.actions import (
    add_mcp_server,
    remove_mcp_server,
    list_mcp_servers,
)


class TestAddMcpServer:
    """Tests for add_mcp_server function."""

    @pytest.mark.asyncio
    async def test_add_http_server_success(self):
        """Test successfully adding an HTTP server."""
        mock_manager = AsyncMock()
        mock_manager.add_server = AsyncMock(
            return_value={
                "success": True,
                "name": "test-server",
                "connected": True,
                "full_tool_schemas": [],
            }
        )

        result = await add_mcp_server(
            mcp_manager=mock_manager,
            name="Test-Server",  # Should be lowercased
            transport="http",
            project_id="project-123",
            url="http://localhost:8080/mcp",
        )

        assert result["success"] is True
        assert result["name"] == "test-server"
        mock_manager.add_server.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_stdio_server_success(self):
        """Test successfully adding a stdio server."""
        mock_manager = AsyncMock()
        mock_manager.add_server = AsyncMock(
            return_value={
                "success": True,
                "name": "context7",
                "connected": True,
                "full_tool_schemas": [],
            }
        )

        result = await add_mcp_server(
            mcp_manager=mock_manager,
            name="context7",
            transport="stdio",
            project_id="project-456",
            command="uvx",
            args=["context7-mcp"],
        )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_add_server_normalizes_name(self):
        """Test that server name is normalized to lowercase."""
        mock_manager = AsyncMock()
        mock_manager.add_server = AsyncMock(
            return_value={"success": True, "name": "myserver", "full_tool_schemas": []}
        )

        await add_mcp_server(
            mcp_manager=mock_manager,
            name="MyServer",
            transport="http",
            project_id="project-789",
            url="http://localhost:8080",
        )

        # Verify the config was created with lowercase name
        call_args = mock_manager.add_server.call_args
        config = call_args[0][0]
        assert config.name == "myserver"

    @pytest.mark.asyncio
    async def test_add_server_failure(self):
        """Test handling server add failure."""
        mock_manager = AsyncMock()
        mock_manager.add_server = AsyncMock(
            return_value={"success": False, "error": "Connection failed"}
        )

        result = await add_mcp_server(
            mcp_manager=mock_manager,
            name="failing-server",
            transport="http",
            project_id="project-123",
            url="http://localhost:9999",
        )

        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_add_server_exception(self):
        """Test handling exception during add."""
        mock_manager = AsyncMock()
        mock_manager.add_server = AsyncMock(side_effect=Exception("Network error"))

        result = await add_mcp_server(
            mcp_manager=mock_manager,
            name="error-server",
            transport="http",
            project_id="project-123",
            url="http://localhost:8080",
        )

        assert result["success"] is False
        assert "Network error" in result["error"]
        assert result["name"] == "error-server"

    @pytest.mark.asyncio
    async def test_add_server_generates_description(self):
        """Test that description is generated if not provided."""
        mock_manager = AsyncMock()
        mock_manager.add_server = AsyncMock(
            return_value={
                "success": True,
                "name": "test-server",
                "full_tool_schemas": [{"name": "tool1", "description": "A tool"}],
            }
        )

        with patch(
            "gobby.mcp_proxy.actions.generate_server_description", new_callable=AsyncMock
        ) as mock_gen:
            mock_gen.return_value = "Generated description"

            result = await add_mcp_server(
                mcp_manager=mock_manager,
                name="test-server",
                transport="http",
                project_id="project-123",
                url="http://localhost:8080",
            )

        # Description generation should be attempted
        mock_gen.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_server_with_custom_description(self):
        """Test adding server with custom description skips generation."""
        mock_manager = AsyncMock()
        mock_manager.add_server = AsyncMock(
            return_value={
                "success": True,
                "name": "test-server",
                "full_tool_schemas": [{"name": "tool1", "description": "A tool"}],
            }
        )

        with patch(
            "gobby.mcp_proxy.actions.generate_server_description", new_callable=AsyncMock
        ) as mock_gen:
            result = await add_mcp_server(
                mcp_manager=mock_manager,
                name="test-server",
                transport="http",
                project_id="project-123",
                url="http://localhost:8080",
                description="My custom description",
            )

        # Should not generate description since one was provided
        mock_gen.assert_not_called()


class TestRemoveMcpServer:
    """Tests for remove_mcp_server function."""

    @pytest.mark.asyncio
    async def test_remove_server_success(self):
        """Test successfully removing a server."""
        mock_manager = AsyncMock()
        mock_manager.remove_server = AsyncMock(return_value={"success": True})

        result = await remove_mcp_server(
            mcp_manager=mock_manager, name="test-server", project_id="project-123"
        )

        assert result["success"] is True
        mock_manager.remove_server.assert_called_once_with("test-server", project_id="project-123")

    @pytest.mark.asyncio
    async def test_remove_server_not_found(self):
        """Test removing non-existent server."""
        mock_manager = AsyncMock()
        mock_manager.remove_server = AsyncMock(
            return_value={"success": False, "error": "Server not found"}
        )

        result = await remove_mcp_server(
            mcp_manager=mock_manager, name="nonexistent", project_id="project-123"
        )

        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_remove_server_exception(self):
        """Test handling exception during remove."""
        mock_manager = AsyncMock()
        mock_manager.remove_server = AsyncMock(side_effect=Exception("Database error"))

        result = await remove_mcp_server(
            mcp_manager=mock_manager, name="error-server", project_id="project-123"
        )

        assert result["success"] is False
        assert "Database error" in result["error"]
        assert result["name"] == "error-server"


class TestListMcpServers:
    """Tests for list_mcp_servers function."""

    @pytest.mark.asyncio
    async def test_list_servers_empty(self):
        """Test listing when no servers configured."""
        mock_manager = MagicMock()
        mock_manager.server_configs = []
        mock_manager.connections = {}
        mock_manager.health = {}

        result = await list_mcp_servers(mock_manager)

        assert result["success"] is True
        assert result["servers"] == []
        assert result["total_count"] == 0
        assert result["connected_count"] == 0

    @pytest.mark.asyncio
    async def test_list_servers_with_servers(self):
        """Test listing multiple servers."""
        mock_config1 = MagicMock()
        mock_config1.name = "server1"
        mock_config1.project_id = "project-123"
        mock_config1.transport = "http"
        mock_config1.enabled = True
        mock_config1.url = "http://localhost:8080"
        mock_config1.command = None
        mock_config1.description = "Server 1"
        mock_config1.tools = [{"name": "tool1"}]

        mock_config2 = MagicMock()
        mock_config2.name = "server2"
        mock_config2.project_id = None  # Global server
        mock_config2.transport = "stdio"
        mock_config2.enabled = True
        mock_config2.url = None
        mock_config2.command = "uvx"
        mock_config2.description = None
        mock_config2.tools = []

        mock_health1 = MagicMock()
        mock_health1.state.value = "connected"

        mock_manager = MagicMock()
        mock_manager.server_configs = [mock_config1, mock_config2]
        mock_manager.connections = {"server1": MagicMock()}  # server1 connected
        mock_manager.health = {"server1": mock_health1}

        result = await list_mcp_servers(mock_manager)

        assert result["success"] is True
        assert len(result["servers"]) == 2
        assert result["total_count"] == 2
        assert result["connected_count"] == 1

        # Check server1 details
        server1 = next(s for s in result["servers"] if s["name"] == "server1")
        assert server1["project_id"] == "project-123"
        assert server1["transport"] == "http"
        assert server1["connected"] is True
        assert server1["state"] == "connected"

        # Check server2 details
        server2 = next(s for s in result["servers"] if s["name"] == "server2")
        assert server2["project_id"] is None
        assert server2["connected"] is False
        assert server2["state"] == "unknown"

    @pytest.mark.asyncio
    async def test_list_servers_exception(self):
        """Test handling exception during list."""
        mock_manager = MagicMock()
        mock_manager.server_configs = MagicMock()
        mock_manager.server_configs.__iter__ = MagicMock(side_effect=Exception("Database error"))

        result = await list_mcp_servers(mock_manager)

        assert result["success"] is False
        assert "Database error" in result["error"]
        assert result["servers"] == []
