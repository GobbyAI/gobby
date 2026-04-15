"""Tests for tmux-backed session MCP terminal tools."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._terminal import _resolve_tmux_target, register_terminal_tools

pytestmark = pytest.mark.unit


class _TestRegistry(InternalToolRegistry):
    """Registry subclass with get_tool for testing."""

    def get_tool(self, name: str) -> Callable[..., Any] | None:
        tool = self._tools.get(name)
        return tool.func if tool else None


class TestResolveTmuxTarget:
    """Tests for session-to-tmux target resolution."""

    def test_returns_error_when_session_missing(self) -> None:
        """Missing sessions should not return a stale default-server marker."""
        session_manager = MagicMock()
        session_manager.get.return_value = None

        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        target, tmux_manager, error = _resolve_tmux_target(
            "missing-session",
            session_manager,
            agent_run_manager,
        )

        assert target is None
        assert tmux_manager is None
        assert error == "Session missing-session not found"


class TestRegisterTerminalTools:
    """Tests for terminal interaction tool registration."""

    def test_send_keys_uses_tmux_manager_for_recorded_socket(self) -> None:
        """Interactive sessions should route through the manager for their recorded tmux server."""
        registry = _TestRegistry(name="test", description="test")

        session = MagicMock()
        session.terminal_context = {
            "tmux_pane": "%12",
            "tmux_socket_path": "/tmp/tmux-1000/gobby",
        }

        session_manager = MagicMock()
        session_manager.get.return_value = session

        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        tmux_manager = MagicMock()
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with (
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
                return_value=agent_run_manager,
            ),
        ):
            register_terminal_tools(registry, session_manager, MagicMock())

        send_keys = registry.get_tool("send_keys")
        assert send_keys is not None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.get_tmux_manager_for_context",
            return_value=tmux_manager,
        ) as mock_get_tmux_manager:
            result = asyncio.run(send_keys(session_id="session-1", keys="hello"))

        assert result == {"success": True}
        mock_get_tmux_manager.assert_called_once_with(session.terminal_context)
        tmux_manager.send_keys.assert_awaited_once_with("%12", "hello", literal=True)
