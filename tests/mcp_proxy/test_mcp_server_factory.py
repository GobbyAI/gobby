"""Tests for MCP server factory functions (create_mcp_server, create_stdio_mcp_server).

Verifies that FastMCP instances are created with the instructions parameter.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


class TestCreateMcpServer:
    """Test create_mcp_server in server.py."""

    def test_fastmcp_receives_instructions(self) -> None:
        """Verify FastMCP is created with instructions parameter."""
        from gobby.mcp_proxy.instructions import build_gobby_instructions

        with patch("gobby.mcp_proxy.server.FastMCP") as mock_fastmcp:
            mock_fastmcp.return_value = MagicMock()

            # Import and call after patching
            from gobby.mcp_proxy.server import create_mcp_server

            mock_tools_handler = MagicMock()
            create_mcp_server(mock_tools_handler)

            # Verify FastMCP was called with instructions
            mock_fastmcp.assert_called_once()
            call_kwargs = mock_fastmcp.call_args
            # FastMCP("gobby", instructions=...)
            assert call_kwargs[0][0] == "gobby"  # First positional arg
            assert "instructions" in call_kwargs[1]  # Has instructions kwarg
            # Verify instructions content
            instructions = call_kwargs[1]["instructions"]
            assert "<gobby_system>" in instructions
            assert build_gobby_instructions() == instructions


class TestCreateStdioMcpServer:
    """Test create_stdio_mcp_server in stdio.py."""

    def test_fastmcp_receives_instructions(self) -> None:
        """Verify stdio FastMCP is created with instructions parameter."""
        from gobby.mcp_proxy.instructions import build_gobby_instructions

        with (
            patch("gobby.mcp_proxy.stdio.FastMCP") as mock_fastmcp,
            patch("gobby.mcp_proxy.stdio.CliRuntime") as mock_runtime,
            patch("gobby.mcp_proxy.stdio.DaemonProxy"),
            patch("gobby.mcp_proxy.stdio.setup_internal_registries"),
        ):
            mock_fastmcp.return_value = MagicMock()
            runtime = MagicMock()
            runtime.require_config.return_value = MagicMock(daemon_port=8787)
            mock_runtime.return_value = runtime

            # Import and call after patching
            from gobby.mcp_proxy.stdio import create_stdio_mcp_server

            create_stdio_mcp_server()

            # Verify FastMCP was called with instructions
            mock_fastmcp.assert_called_once()
            call_kwargs = mock_fastmcp.call_args
            # FastMCP("gobby", instructions=...)
            assert call_kwargs[0][0] == "gobby"  # First positional arg
            assert "instructions" in call_kwargs[1]  # Has instructions kwarg
            # Verify instructions content
            instructions = call_kwargs[1]["instructions"]
            assert "<gobby_system>" in instructions
            assert build_gobby_instructions() == instructions
