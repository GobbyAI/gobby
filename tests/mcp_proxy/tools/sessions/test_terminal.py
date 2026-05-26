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

    def test_accepts_json_terminal_context(self) -> None:
        """Stored terminal_context may be raw JSON text."""
        session = MagicMock()
        session.terminal_context = '{"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"}'

        session_manager = MagicMock()
        session_manager.get.return_value = session

        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.get_tmux_manager_for_context"
        ) as mock_get_tmux_manager:
            target, tmux_manager, error = _resolve_tmux_target(
                "session-1",
                session_manager,
                agent_run_manager,
            )

        assert target == "%12"
        assert tmux_manager == mock_get_tmux_manager.return_value
        assert error is None
        mock_get_tmux_manager.assert_called_once_with(
            {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"}
        )

    def test_accepts_mapping_terminal_context(self) -> None:
        """Stored terminal_context may already be a parsed mapping."""
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"}

        session_manager = MagicMock()
        session_manager.get.return_value = session

        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.get_tmux_manager_for_context"
        ) as mock_get_tmux_manager:
            target, tmux_manager, error = _resolve_tmux_target(
                "session-1",
                session_manager,
                agent_run_manager,
            )

        assert target == "%12"
        assert tmux_manager == mock_get_tmux_manager.return_value
        assert error is None
        mock_get_tmux_manager.assert_called_once_with(session.terminal_context)

    def test_reports_invalid_terminal_context(self) -> None:
        """Malformed stored terminal_context returns a useful diagnostic."""
        session = MagicMock()
        session.terminal_context = "{not json"

        session_manager = MagicMock()
        session_manager.get.return_value = session

        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        target, tmux_manager, error = _resolve_tmux_target(
            "session-1",
            session_manager,
            agent_run_manager,
        )

        assert target is None
        assert tmux_manager is None
        assert error == (
            "Session session-1 has invalid terminal_context (str); expected object or JSON object"
        )

    def test_reports_terminal_context_without_tmux_target(self) -> None:
        """A parsed context without a tmux target should explain its keys."""
        session = MagicMock()
        session.terminal_context = {"terminal": "tmux"}

        session_manager = MagicMock()
        session_manager.get.return_value = session

        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        target, tmux_manager, error = _resolve_tmux_target(
            "session-1",
            session_manager,
            agent_run_manager,
        )

        assert target is None
        assert tmux_manager is None
        assert error == (
            "Session session-1 terminal_context has no tmux_pane or tmux_session (keys: terminal)"
        )


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

    def test_capture_output_uses_tmux_when_pane_exists(self) -> None:
        """capture_output reads the live pane when a tmux target is available."""
        registry = _TestRegistry(name="test", description="test")
        session = MagicMock()
        session.terminal_context = {"tmux_pane": "%12"}

        session_manager = MagicMock()
        session_manager.get.return_value = session
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None
        tmux_manager = MagicMock()
        tmux_manager.capture_pane = AsyncMock(return_value="live output")

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, MagicMock())

        capture_output = registry.get_tool("capture_output")
        assert capture_output is not None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.get_tmux_manager_for_context",
            return_value=tmux_manager,
        ):
            result = asyncio.run(capture_output(session_id="session-1", lines=20))

        assert result == {"success": True, "output": "live output", "via": "tmux"}
        tmux_manager.capture_pane.assert_awaited_once_with("%12", 20)

    def test_capture_output_falls_back_to_transcript_tail(self, tmp_path) -> None:
        """When no tmux target exists, capture_output returns a transcript tail."""
        registry = _TestRegistry(name="test", description="test")
        transcript = tmp_path / "codex.jsonl"
        transcript.write_text("one\n-two\nthree\n", encoding="utf-8")

        session = MagicMock()
        session.terminal_context = {"parent_pid": 12345}
        session.transcript_path = str(transcript)

        session_manager = MagicMock()
        session_manager.get.return_value = session
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, MagicMock())

        capture_output = registry.get_tool("capture_output")
        assert capture_output is not None

        result = asyncio.run(capture_output(session_id="session-1", lines=2))

        assert result["success"] is True
        assert result["via"] == "transcript"
        assert result["output"] == "-two\nthree"
        assert "No live tmux pane" in result["note"]

    def test_capture_output_reports_no_pane_or_transcript(self) -> None:
        """Missing tmux target plus missing transcript returns structured failure."""
        registry = _TestRegistry(name="test", description="test")
        session = MagicMock()
        session.terminal_context = {"parent_pid": 12345}
        session.transcript_path = None

        session_manager = MagicMock()
        session_manager.get.return_value = session
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, MagicMock())

        capture_output = registry.get_tool("capture_output")
        assert capture_output is not None

        result = asyncio.run(capture_output(session_id="session-1", lines=20))

        assert result["success"] is False
        assert result["error_code"] == "no_live_pane_or_transcript"
        assert result["transcript_error"] == "missing_transcript_path"
