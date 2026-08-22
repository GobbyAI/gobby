"""Tests for MCP server factory functions (create_mcp_server, create_stdio_mcp_server).

Verifies that MCPServer instances are created with Gobby's instructions and
advertise the package version.
"""

from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.utils.version import get_version

pytestmark = pytest.mark.unit


class TestCreateMcpServer:
    """Test create_mcp_server in server.py."""

    def test_mcpserver_receives_instructions_and_version(self) -> None:
        """Verify MCPServer is created with instructions and the package version."""
        from gobby.mcp_proxy.instructions import build_gobby_instructions

        with patch("gobby.mcp_proxy.server.MCPServer") as mock_server:
            mock_server.return_value = MagicMock()

            # Import and call after patching
            from gobby.mcp_proxy.server import create_mcp_server

            mock_tools_handler = MagicMock()
            create_mcp_server(mock_tools_handler)

            mock_server.assert_called_once()
            call_kwargs = mock_server.call_args
            # MCPServer("gobby", instructions=..., version=...)
            assert call_kwargs[0][0] == "gobby"  # First positional arg
            instructions = call_kwargs[1]["instructions"]
            assert "<gobby_system>" in instructions
            assert build_gobby_instructions() == instructions
            assert call_kwargs[1]["version"] == get_version()

    def test_real_mcpserver_advertises_gobby_version(self) -> None:
        """The real server object carries name, instructions, and version for serverInfo."""
        from types import SimpleNamespace

        from gobby.mcp_proxy.server import GobbyDaemonTools, create_mcp_server

        async def _tool(**kwargs: object) -> dict[str, object]:
            return {}

        tool_names = (
            "status",
            "list_mcp_servers",
            "call_tool",
            "list_tools",
            "get_tool_schema",
            "read_mcp_resource",
            "add_mcp_server",
            "remove_mcp_server",
            "import_mcp_server",
            "recommend_tools",
            "search_tools",
            "set_variable",
            "get_variable",
        )
        # Intentional invalid-input boundary: a namespace of real async callables
        # stands in for the tools handler so add_tool can introspect signatures.
        handler = cast(GobbyDaemonTools, SimpleNamespace(**dict.fromkeys(tool_names, _tool)))
        server = create_mcp_server(handler)

        assert server.name == "gobby"
        assert server.version == get_version()
        assert server.instructions is not None and "<gobby_system>" in server.instructions


class TestCreateStdioMcpServer:
    """Test create_stdio_mcp_server in stdio.py."""

    def test_mcpserver_receives_instructions_and_version(self) -> None:
        """Verify stdio MCPServer is created with instructions, version, and lifespan."""
        from gobby.mcp_proxy.instructions import build_gobby_instructions

        with (
            patch("gobby.mcp_proxy.stdio._StdioMCPServer") as mock_server,
            patch("gobby.mcp_proxy.stdio.CliRuntime") as mock_runtime,
            patch("gobby.mcp_proxy.stdio.DaemonProxy"),
            patch("gobby.mcp_proxy.stdio.setup_internal_registries"),
        ):
            mock_server.return_value = MagicMock()
            runtime = MagicMock()
            runtime.require_config.return_value = MagicMock(daemon_port=8787)
            mock_runtime.return_value = runtime

            # Import and call after patching
            from gobby.mcp_proxy.stdio import create_stdio_mcp_server

            create_stdio_mcp_server()

            mock_server.assert_called_once()
            call_kwargs = mock_server.call_args
            # _StdioMCPServer("gobby", instructions=..., version=..., lifespan=...)
            assert call_kwargs[0][0] == "gobby"  # First positional arg
            instructions = call_kwargs[1]["instructions"]
            assert "<gobby_system>" in instructions
            assert build_gobby_instructions() == instructions
            assert call_kwargs[1]["version"] == get_version()
            assert callable(call_kwargs[1]["lifespan"])
