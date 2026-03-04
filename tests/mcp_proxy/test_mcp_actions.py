"""Tests for src/mcp_proxy/actions.py - MCP Actions."""

from unittest.mock import AsyncMock, patch

import pytest

from gobby.mcp_proxy.actions import (
    add_mcp_server,
    remove_mcp_server,
)

pytestmark = pytest.mark.unit


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

            await add_mcp_server(
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
            await add_mcp_server(
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
