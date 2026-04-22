"""Additional coverage tests for GobbyDaemonTools in server.py."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.server import GobbyDaemonTools

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_mcp_manager():
    """Create a mock MCP client manager."""
    manager = MagicMock()
    manager.project_id = "test-project-id"
    manager.connections = {}
    manager.health = {}
    manager.server_configs = []
    manager.get_server_health.return_value = {}
    manager.get_lazy_connection_states.return_value = {}
    return manager


@pytest.fixture
def mock_internal_manager():
    """Create a mock internal tool manager."""
    return MagicMock()


class TestSearchToolsExceptionHandling:
    """Tests for search_tools exception handling (lines 285-287)."""

    @pytest.mark.asyncio
    async def test_search_tools_exception_returns_error(
        self, mock_mcp_manager, mock_internal_manager
    ):
        """Test that exceptions in semantic search are caught and returned."""
        mock_semantic = AsyncMock()
        mock_semantic.search_tools = AsyncMock(side_effect=RuntimeError("Embedding model failed"))

        handler = GobbyDaemonTools(
            mcp_manager=mock_mcp_manager,
            daemon_port=8787,
            websocket_port=8788,
            start_time=1000.0,
            internal_manager=mock_internal_manager,
            semantic_search=mock_semantic,
        )

        result = await handler.search_tools(query="find files")

        assert result["success"] is False
        assert "Embedding model failed" in result["error"]
        assert result["query"] == "find files"


# TestListHookHandlers, TestTestHookEvent, TestListPlugins, TestReloadPlugin
# moved to tests/mcp_proxy/tools/test_plugins.py (gobby-plugins internal registry)


class TestCallToolSessionResolution:
    """call_tool() must resolve external_id refs and propagate the platform UUID."""

    def _make_handler(self, *, resolve_to: str | None = None, resolve_exc=None):
        session_manager = MagicMock()
        session_manager.db = MagicMock()
        if resolve_exc is not None:
            session_manager.resolve_session_reference.side_effect = resolve_exc
        else:
            session_manager.resolve_session_reference.return_value = resolve_to
        session = MagicMock()
        session.external_id = "ext-xyz"
        session.project_id = "proj-abc"
        session_manager.get.return_value = session

        mcp_manager = MagicMock()
        mcp_manager.project_id = "test-project-id"
        mcp_manager.connections = {}
        mcp_manager.health = {}
        mcp_manager.server_configs = []

        handler = GobbyDaemonTools(
            mcp_manager=mcp_manager,
            daemon_port=8787,
            websocket_port=8788,
            start_time=1000.0,
            internal_manager=MagicMock(),
            session_manager=session_manager,
        )
        handler.tool_proxy = MagicMock()
        handler.tool_proxy.call_tool = AsyncMock(return_value={"ok": True})
        return handler, session_manager

    @pytest.mark.asyncio
    async def test_call_tool_resolves_external_id_to_platform_uuid(self) -> None:
        """External_id argument resolves to platform UUID before hitting tool_proxy.call_tool."""
        handler, _ = self._make_handler(resolve_to="platform-uuid-7")

        await handler.call_tool(
            server_name="gobby-tasks",
            tool_name="suggest_next_task",
            arguments={"parent_task_id": "#1"},
            session_id="11111111-1111-1111-1111-111111111111",
        )

        # tool_proxy.call_tool takes session_id as the 4th positional arg today
        # (signature: call_tool(server_name, tool_name, arguments, session_id)).
        # Prefer the kwargs lookup when present so the test doesn't break if the
        # call site switches to keyword arguments.
        call_args = handler.tool_proxy.call_tool.call_args
        resolved = call_args.kwargs.get("session_id")
        if resolved is None:
            resolved = call_args.args[3]
        assert resolved == "platform-uuid-7"

    @pytest.mark.asyncio
    async def test_call_tool_skips_session_context_when_unresolvable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unresolvable session ref → warning + no SessionContext planted."""
        handler, _ = self._make_handler(
            resolve_to=None, resolve_exc=ValueError("Session not found")
        )
        caplog.set_level(logging.WARNING, logger="gobby.utils.session_context")

        await handler.call_tool(
            server_name="gobby-tasks",
            tool_name="suggest_next_task",
            arguments={},
            session_id="22222222-2222-2222-2222-222222222222",
        )

        assert any("could not resolve session ref" in rec.message for rec in caplog.records)
        call_args = handler.tool_proxy.call_tool.call_args
        resolved = call_args.kwargs.get("session_id")
        if resolved is None and len(call_args.args) > 3:
            resolved = call_args.args[3]
        assert resolved is None
