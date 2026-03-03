"""
Comprehensive tests for src/gobby/mcp_proxy/actions.py - MCP Actions module.

This module tests:
- add_mcp_server: Adding HTTP, stdio, and websocket servers
- remove_mcp_server: Removing servers with various scenarios
- list_mcp_servers: Listing servers with different states
- Error handling and edge cases
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.actions import (
    add_mcp_server,
    remove_mcp_server,
)
from gobby.mcp_proxy.manager import MCPServerConfig

pytestmark = pytest.mark.unit

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_mcp_manager():
    """Create a mock MCP client manager."""
    manager = MagicMock()
    manager.add_server = AsyncMock()
    manager.remove_server = AsyncMock()
    manager.server_configs = []
    manager.connections = {}
    manager.health = {}
    return manager


@pytest.fixture
def sample_server_config():
    """Create a sample server config for testing."""
    config = MagicMock(spec=MCPServerConfig)
    config.name = "test-server"
    config.project_id = "project-123"
    config.transport = "http"
    config.enabled = True
    config.url = "http://localhost:8080"
    config.command = None
    config.description = "Test server description"
    config.tools = [{"name": "tool1", "brief": "A tool"}]
    return config


@pytest.fixture
def sample_health_status():
    """Create a sample health status."""
    health = MagicMock()
    health.state.value = "connected"
    return health


# =============================================================================
# Tests for add_mcp_server
# =============================================================================


class TestAddMcpServer:
    """Tests for the add_mcp_server function."""

    @pytest.mark.asyncio
    async def test_add_http_server_success(self, mock_mcp_manager):
        """Test successfully adding an HTTP server."""
        mock_mcp_manager.add_server.return_value = {
            "success": True,
            "name": "test-server",
            "connected": True,
            "full_tool_schemas": [],
        }

        result = await add_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="Test-Server",
            transport="http",
            project_id="project-123",
            url="http://localhost:8080/mcp",
            headers={"Authorization": "Bearer token"},
        )

        assert result["success"] is True
        assert result["name"] == "test-server"
        mock_mcp_manager.add_server.assert_called_once()

        # Verify the config was created correctly
        call_args = mock_mcp_manager.add_server.call_args
        config = call_args[0][0]
        assert config.name == "test-server"
        assert config.transport == "http"
        assert config.url == "http://localhost:8080/mcp"
        assert config.headers == {"Authorization": "Bearer token"}

    @pytest.mark.asyncio
    async def test_add_stdio_server_success(self, mock_mcp_manager):
        """Test successfully adding a stdio server."""
        mock_mcp_manager.add_server.return_value = {
            "success": True,
            "name": "context7",
            "connected": True,
            "full_tool_schemas": [],
        }

        result = await add_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="context7",
            transport="stdio",
            project_id="project-456",
            command="uvx",
            args=["context7-mcp"],
            env={"DEBUG": "true"},
        )

        assert result["success"] is True
        assert result["name"] == "context7"

        # Verify the config was created correctly
        call_args = mock_mcp_manager.add_server.call_args
        config = call_args[0][0]
        assert config.command == "uvx"
        assert config.args == ["context7-mcp"]
        assert config.env == {"DEBUG": "true"}

    @pytest.mark.asyncio
    async def test_add_websocket_server_success(self, mock_mcp_manager):
        """Test successfully adding a websocket server."""
        mock_mcp_manager.add_server.return_value = {
            "success": True,
            "name": "ws-server",
            "connected": True,
            "full_tool_schemas": [],
        }

        result = await add_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="ws-server",
            transport="websocket",
            project_id="project-789",
            url="ws://localhost:8080/mcp",
        )

        assert result["success"] is True
        assert result["name"] == "ws-server"

    @pytest.mark.asyncio
    async def test_add_server_normalizes_name_to_lowercase(self, mock_mcp_manager):
        """Test that server name is normalized to lowercase."""
        mock_mcp_manager.add_server.return_value = {
            "success": True,
            "name": "myserver",
            "full_tool_schemas": [],
        }

        await add_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="MyServer",
            transport="http",
            project_id="project-123",
            url="http://localhost:8080",
        )

        # Verify the config was created with lowercase name
        call_args = mock_mcp_manager.add_server.call_args
        config = call_args[0][0]
        assert config.name == "myserver"

    @pytest.mark.asyncio
    async def test_add_server_normalizes_mixed_case_name(self, mock_mcp_manager):
        """Test name normalization with mixed case."""
        mock_mcp_manager.add_server.return_value = {
            "success": True,
            "name": "my-test-server",
            "full_tool_schemas": [],
        }

        await add_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="My-TEST-Server",
            transport="http",
            project_id="project-123",
            url="http://localhost:8080",
        )

        call_args = mock_mcp_manager.add_server.call_args
        config = call_args[0][0]
        assert config.name == "my-test-server"

    @pytest.mark.asyncio
    async def test_add_server_with_disabled_flag(self, mock_mcp_manager):
        """Test adding a disabled server."""
        mock_mcp_manager.add_server.return_value = {
            "success": True,
            "name": "disabled-server",
            "full_tool_schemas": [],
        }

        await add_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="disabled-server",
            transport="http",
            project_id="project-123",
            url="http://localhost:8080",
            enabled=False,
        )

        call_args = mock_mcp_manager.add_server.call_args
        config = call_args[0][0]
        assert config.enabled is False

    @pytest.mark.asyncio
    async def test_add_server_failure_returns_error(self, mock_mcp_manager):
        """Test handling server add failure from manager."""
        mock_mcp_manager.add_server.return_value = {
            "success": False,
            "error": "Connection refused",
        }

        result = await add_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="failing-server",
            transport="http",
            project_id="project-123",
            url="http://localhost:9999",
        )

        assert result["success"] is False
        assert result.get("error") == "Connection refused"

    @pytest.mark.asyncio
    async def test_add_server_exception_returns_error_dict(self, mock_mcp_manager):
        """Test handling exception during add returns structured error."""
        mock_mcp_manager.add_server.side_effect = Exception("Network error")

        result = await add_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="error-server",
            transport="http",
            project_id="project-123",
            url="http://localhost:8080",
        )

        assert result["success"] is False
        assert "Network error" in result["error"]
        assert result["name"] == "error-server"
        assert "Failed to add server" in result["message"]

    @pytest.mark.asyncio
    async def test_add_server_value_error_exception(self, mock_mcp_manager):
        """Test handling ValueError exception."""
        mock_mcp_manager.add_server.side_effect = ValueError("Invalid config")

        result = await add_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="invalid-server",
            transport="http",
            project_id="project-123",
            url="http://localhost:8080",
        )

        assert result["success"] is False
        assert "Invalid config" in result["error"]

    @pytest.mark.asyncio
    async def test_add_server_generates_description_from_tools(self, mock_mcp_manager):
        """Test that description is generated when tools are returned."""
        mock_mcp_manager.add_server.return_value = {
            "success": True,
            "name": "test-server",
            "full_tool_schemas": [
                {"name": "tool1", "description": "First tool"},
                {"name": "tool2", "description": "Second tool"},
            ],
        }

        with patch(
            "gobby.mcp_proxy.actions.generate_server_description",
            new_callable=AsyncMock,
        ) as mock_gen:
            mock_gen.return_value = "Generated description"

            await add_mcp_server(
                mcp_manager=mock_mcp_manager,
                name="test-server",
                transport="http",
                project_id="project-123",
                url="http://localhost:8080",
            )

            mock_gen.assert_called_once_with(
                server_name="test-server",
                tool_summaries=[
                    {"name": "tool1", "description": "First tool"},
                    {"name": "tool2", "description": "Second tool"},
                ],
            )

    @pytest.mark.asyncio
    async def test_add_server_skips_description_generation_when_provided(self, mock_mcp_manager):
        """Test that description generation is skipped when custom description is provided."""
        mock_mcp_manager.add_server.return_value = {
            "success": True,
            "name": "test-server",
            "full_tool_schemas": [{"name": "tool1", "description": "A tool"}],
        }

        with patch(
            "gobby.mcp_proxy.actions.generate_server_description",
            new_callable=AsyncMock,
        ) as mock_gen:
            await add_mcp_server(
                mcp_manager=mock_mcp_manager,
                name="test-server",
                transport="http",
                project_id="project-123",
                url="http://localhost:8080",
                description="My custom description",
            )

            mock_gen.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_server_skips_description_generation_when_no_tools(self, mock_mcp_manager):
        """Test that description generation is skipped when no tools returned."""
        mock_mcp_manager.add_server.return_value = {
            "success": True,
            "name": "test-server",
            "full_tool_schemas": [],
        }

        with patch(
            "gobby.mcp_proxy.actions.generate_server_description",
            new_callable=AsyncMock,
        ) as mock_gen:
            await add_mcp_server(
                mcp_manager=mock_mcp_manager,
                name="test-server",
                transport="http",
                project_id="project-123",
                url="http://localhost:8080",
            )

            mock_gen.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_server_handles_description_generation_failure(self, mock_mcp_manager):
        """Test that description generation failure doesn't fail the add operation."""
        mock_mcp_manager.add_server.return_value = {
            "success": True,
            "name": "test-server",
            "full_tool_schemas": [{"name": "tool1", "description": "A tool"}],
        }

        with patch(
            "gobby.mcp_proxy.actions.generate_server_description",
            new_callable=AsyncMock,
        ) as mock_gen:
            mock_gen.side_effect = Exception("AI service unavailable")

            result = await add_mcp_server(
                mcp_manager=mock_mcp_manager,
                name="test-server",
                transport="http",
                project_id="project-123",
                url="http://localhost:8080",
            )

            # Add should still succeed even if description generation fails
            assert result["success"] is True

    @pytest.mark.asyncio
    async def test_add_server_with_all_optional_params(self, mock_mcp_manager):
        """Test adding server with all optional parameters."""
        mock_mcp_manager.add_server.return_value = {
            "success": True,
            "name": "full-server",
            "full_tool_schemas": [],
        }

        await add_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="full-server",
            transport="stdio",
            project_id="project-123",
            url=None,
            headers=None,
            command="/usr/bin/server",
            args=["--verbose", "--port", "8080"],
            env={"HOME": "/home/user", "PATH": "/usr/bin"},
            enabled=True,
            description="Fully configured server",
        )

        call_args = mock_mcp_manager.add_server.call_args
        config = call_args[0][0]
        assert config.command == "/usr/bin/server"
        assert config.args == ["--verbose", "--port", "8080"]
        assert config.env == {"HOME": "/home/user", "PATH": "/usr/bin"}
        assert config.description == "Fully configured server"


# =============================================================================
# Tests for remove_mcp_server
# =============================================================================


class TestRemoveMcpServer:
    """Tests for the remove_mcp_server function."""

    @pytest.mark.asyncio
    async def test_remove_server_success(self, mock_mcp_manager):
        """Test successfully removing a server."""
        mock_mcp_manager.remove_server.return_value = {"success": True}

        result = await remove_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="test-server",
            project_id="project-123",
        )

        assert result["success"] is True
        mock_mcp_manager.remove_server.assert_called_once_with(
            "test-server", project_id="project-123"
        )

    @pytest.mark.asyncio
    async def test_remove_server_not_found(self, mock_mcp_manager):
        """Test removing non-existent server."""
        mock_mcp_manager.remove_server.return_value = {
            "success": False,
            "error": "Server not found",
        }

        result = await remove_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="nonexistent",
            project_id="project-123",
        )

        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_remove_server_exception_returns_error_dict(self, mock_mcp_manager):
        """Test handling exception during remove."""
        mock_mcp_manager.remove_server.side_effect = Exception("Database error")

        result = await remove_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="error-server",
            project_id="project-123",
        )

        assert result["success"] is False
        assert "Database error" in result["error"]
        assert result["name"] == "error-server"
        assert "Failed to remove server" in result["message"]

    @pytest.mark.asyncio
    async def test_remove_server_value_error_exception(self, mock_mcp_manager):
        """Test handling ValueError exception during remove."""
        mock_mcp_manager.remove_server.side_effect = ValueError("Server 'test' not found")

        result = await remove_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="test",
            project_id="project-123",
        )

        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_remove_server_with_different_project_ids(self, mock_mcp_manager):
        """Test removing servers from different projects."""
        mock_mcp_manager.remove_server.return_value = {"success": True}

        # Remove from project A
        await remove_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="server-a",
            project_id="project-a",
        )
        mock_mcp_manager.remove_server.assert_called_with("server-a", project_id="project-a")

        # Remove from project B
        await remove_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="server-b",
            project_id="project-b",
        )
        mock_mcp_manager.remove_server.assert_called_with("server-b", project_id="project-b")

    @pytest.mark.asyncio
    async def test_remove_server_logs_on_success(self, mock_mcp_manager, caplog):
        """Test that successful removal is logged."""
        mock_mcp_manager.remove_server.return_value = {"success": True}

        import logging

        with caplog.at_level(logging.DEBUG):
            await remove_mcp_server(
                mcp_manager=mock_mcp_manager,
                name="test-server",
                project_id="project-123",
            )

        # The debug log should be present
        assert any("Removed MCP server" in record.message for record in caplog.records)


# =============================================================================
# Edge Cases and Integration Scenarios
# =============================================================================


class TestEdgeCases:
    """Test edge cases and corner scenarios."""

    @pytest.mark.asyncio
    async def test_add_server_with_empty_name(self, mock_mcp_manager):
        """Test adding server with empty name."""
        mock_mcp_manager.add_server.return_value = {
            "success": True,
            "name": "",
            "full_tool_schemas": [],
        }

        result = await add_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="",
            transport="http",
            project_id="project-123",
            url="http://localhost:8080",
        )

        # Empty name should still be processed (validation happens in manager)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_add_server_with_special_characters_in_name(self, mock_mcp_manager):
        """Test server name normalization handles special characters."""
        mock_mcp_manager.add_server.return_value = {
            "success": True,
            "name": "my-server_v2.0",
            "full_tool_schemas": [],
        }

        await add_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="MY-SERVER_V2.0",
            transport="http",
            project_id="project-123",
            url="http://localhost:8080",
        )

        call_args = mock_mcp_manager.add_server.call_args
        config = call_args[0][0]
        assert config.name == "my-server_v2.0"

    @pytest.mark.asyncio
    async def test_add_server_with_unicode_name(self, mock_mcp_manager):
        """Test server name normalization handles unicode."""
        mock_mcp_manager.add_server.return_value = {
            "success": True,
            "name": "server-\u00e9",
            "full_tool_schemas": [],
        }

        await add_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="Server-\u00c9",  # Capital E with acute
            transport="http",
            project_id="project-123",
            url="http://localhost:8080",
        )

        call_args = mock_mcp_manager.add_server.call_args
        config = call_args[0][0]
        # Should be lowercased
        assert config.name == "server-\u00e9"

    @pytest.mark.asyncio
    async def test_remove_server_with_empty_project_id(self, mock_mcp_manager):
        """Test removing server with empty project_id."""
        mock_mcp_manager.remove_server.return_value = {"success": True}

        result = await remove_mcp_server(
            mcp_manager=mock_mcp_manager,
            name="test-server",
            project_id="",
        )

        assert result["success"] is True
        mock_mcp_manager.remove_server.assert_called_once_with("test-server", project_id="")


class TestConcurrencyScenarios:
    """Test concurrent operation scenarios."""

    @pytest.mark.asyncio
    async def test_concurrent_add_operations(self, mock_mcp_manager):
        """Test multiple concurrent add operations."""
        import asyncio

        mock_mcp_manager.add_server.return_value = {
            "success": True,
            "name": "test",
            "full_tool_schemas": [],
        }

        async def add_server(name: str):
            return await add_mcp_server(
                mcp_manager=mock_mcp_manager,
                name=name,
                transport="http",
                project_id="project-123",
                url="http://localhost:8080",
            )

        results = await asyncio.gather(
            add_server("server-1"),
            add_server("server-2"),
            add_server("server-3"),
        )

        assert all(r["success"] for r in results)
        assert mock_mcp_manager.add_server.call_count == 3


class TestLogging:
    """Test logging behavior."""

    @pytest.mark.asyncio
    async def test_add_server_logs_debug_on_success(self, mock_mcp_manager, caplog):
        """Test that successful add is logged at debug level."""
        import logging

        mock_mcp_manager.add_server.return_value = {
            "success": True,
            "name": "test-server",
            "full_tool_schemas": [],
        }

        with caplog.at_level(logging.DEBUG):
            await add_mcp_server(
                mcp_manager=mock_mcp_manager,
                name="test-server",
                transport="http",
                project_id="project-123",
                url="http://localhost:8080",
            )

        assert any("Added MCP server" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_add_server_logs_error_on_exception(self, mock_mcp_manager, caplog):
        """Test that exception is logged at error level."""
        import logging

        mock_mcp_manager.add_server.side_effect = Exception("Connection failed")

        with caplog.at_level(logging.ERROR):
            await add_mcp_server(
                mcp_manager=mock_mcp_manager,
                name="test-server",
                transport="http",
                project_id="project-123",
                url="http://localhost:8080",
            )

        assert any("Failed to add MCP server" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_remove_server_logs_error_on_exception(self, mock_mcp_manager, caplog):
        """Test that remove exception is logged at error level."""
        import logging

        mock_mcp_manager.remove_server.side_effect = Exception("Delete failed")

        with caplog.at_level(logging.ERROR):
            await remove_mcp_server(
                mcp_manager=mock_mcp_manager,
                name="test-server",
                project_id="project-123",
            )

        assert any("Failed to remove MCP server" in record.message for record in caplog.records)
