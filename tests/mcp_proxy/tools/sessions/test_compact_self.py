"""Tests for the compact_self MCP tool.

compact_self fires the appropriate slash command into the calling session's
CLI to trigger context compaction at workflow handoff boundaries (e.g. after
/gobby plan spawns plan-adversary). Terminal sessions go through tmux
send_keys; web_chat sessions return a structured 'follow-up' response until
the daemon-level ChatSession registry lands (see #13684).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._terminal import (
    _CLI_COMPACT_COMMANDS,
    register_terminal_tools,
)

pytestmark = pytest.mark.unit


class _TestRegistry(InternalToolRegistry):
    """Registry subclass with get_tool for testing."""

    def get_tool(self, name: str) -> Callable[..., Any] | None:
        tool = self._tools.get(name)
        return tool.func if tool else None


def _make_terminal_session(source: str, tmux_pane: str | None = "%12") -> MagicMock:
    session = MagicMock()
    session.session_type = "terminal"
    session.source = source
    session.terminal_context = (
        {"tmux_pane": tmux_pane, "tmux_socket_path": "/tmp/tmux"} if tmux_pane else {}
    )
    return session


def _register_compact_self(session: MagicMock, tmux_send_keys_returns: bool = True):
    registry = _TestRegistry(name="test", description="test")
    session_manager = MagicMock()
    session_manager.get.return_value = session

    agent_run_manager = MagicMock()
    agent_run_manager.get_by_session.return_value = None

    tmux_manager = MagicMock()
    tmux_manager.send_keys = AsyncMock(return_value=tmux_send_keys_returns)

    with patch(
        "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
        return_value=agent_run_manager,
    ):
        register_terminal_tools(registry, session_manager, MagicMock())

    return registry, tmux_manager


def _call_compact_self(registry: _TestRegistry, tmux_manager: MagicMock, **kwargs: Any) -> Any:
    compact_self = registry.get_tool("compact_self")
    assert compact_self is not None
    with patch(
        "gobby.mcp_proxy.tools.sessions._terminal.get_tmux_manager_for_context",
        return_value=tmux_manager,
    ):
        return asyncio.run(compact_self(**kwargs))


class TestCompactSelfCLIMap:
    def test_claude_maps_to_slash_compact(self) -> None:
        assert _CLI_COMPACT_COMMANDS["claude"] == "/compact"

    def test_codex_maps_to_slash_compact(self) -> None:
        assert _CLI_COMPACT_COMMANDS["codex"] == "/compact"

    def test_gemini_maps_to_slash_compress(self) -> None:
        assert _CLI_COMPACT_COMMANDS["gemini"] == "/compress"

    def test_qwen_maps_to_slash_compress(self) -> None:
        assert _CLI_COMPACT_COMMANDS["qwen"] == "/compress"

    def test_droid_maps_to_slash_compress(self) -> None:
        assert _CLI_COMPACT_COMMANDS["droid"] == "/compress"


class TestCompactSelfTerminalPath:
    def test_claude_session_fires_slash_compact_via_send_keys(self) -> None:
        session = _make_terminal_session("claude")
        registry, tmux = _register_compact_self(session)

        result = _call_compact_self(registry, tmux, session_id="s1")

        assert result == {
            "compacted": True,
            "command": "/compact",
            "cli": "claude",
            "via": "tmux",
        }
        tmux.send_keys.assert_awaited_once_with("%12", "/compact\n", literal=True)

    def test_codex_session_fires_slash_compact(self) -> None:
        session = _make_terminal_session("codex")
        registry, tmux = _register_compact_self(session)

        result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["compacted"] is True
        assert result["command"] == "/compact"
        tmux.send_keys.assert_awaited_once_with("%12", "/compact\n", literal=True)

    def test_gemini_session_fires_slash_compress(self) -> None:
        session = _make_terminal_session("gemini")
        registry, tmux = _register_compact_self(session)

        result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["compacted"] is True
        assert result["command"] == "/compress"
        tmux.send_keys.assert_awaited_once_with("%12", "/compress\n", literal=True)

    def test_qwen_session_fires_slash_compress(self) -> None:
        session = _make_terminal_session("qwen")
        registry, tmux = _register_compact_self(session)

        result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["command"] == "/compress"

    def test_droid_session_fires_slash_compress(self) -> None:
        session = _make_terminal_session("droid")
        registry, tmux = _register_compact_self(session)

        result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["command"] == "/compress"


class TestCompactSelfFailureModes:
    def test_session_not_found_returns_compacted_false(self) -> None:
        registry = _TestRegistry(name="test", description="test")
        session_manager = MagicMock()
        session_manager.get.return_value = None
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, MagicMock())

        compact_self = registry.get_tool("compact_self")
        assert compact_self is not None
        result = asyncio.run(compact_self(session_id="missing"))

        assert result["compacted"] is False
        assert "not found" in result["reason"]

    def test_unknown_source_returns_compacted_false(self) -> None:
        session = _make_terminal_session("ubergoose")
        registry, tmux = _register_compact_self(session)

        result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["compacted"] is False
        assert "no compaction command known" in result["reason"]
        assert "ubergoose" in result["reason"]
        tmux.send_keys.assert_not_called()

    def test_no_tmux_pane_returns_compacted_false(self) -> None:
        session = _make_terminal_session("claude", tmux_pane=None)
        registry, tmux = _register_compact_self(session)

        result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["compacted"] is False
        assert (
            "no tmux_pane or tmux_session" in result["reason"]
            or "no tmux terminal" in result["reason"]
        )
        tmux.send_keys.assert_not_called()

    def test_tmux_send_keys_failure_returns_compacted_false(self) -> None:
        session = _make_terminal_session("claude")
        registry, tmux = _register_compact_self(session, tmux_send_keys_returns=False)

        result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["compacted"] is False
        assert "tmux send-keys failed" in result["reason"]


class TestCompactSelfWebChatPath:
    def test_web_chat_returns_follow_up_reason(self) -> None:
        session = MagicMock()
        session.session_type = "web_chat"
        session.source = "claude"
        registry = _TestRegistry(name="test", description="test")
        session_manager = MagicMock()
        session_manager.get.return_value = session
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, MagicMock())

        compact_self = registry.get_tool("compact_self")
        assert compact_self is not None
        result = asyncio.run(compact_self(session_id="s1"))

        assert result["compacted"] is False
        assert "web_chat" in result["reason"]
        assert "#13683" in result["reason"]


class TestCompactSelfUnsupportedSessionType:
    def test_unsupported_session_type_returns_compacted_false(self) -> None:
        session = MagicMock()
        session.session_type = "ghost"
        session.source = "claude"
        registry = _TestRegistry(name="test", description="test")
        session_manager = MagicMock()
        session_manager.get.return_value = session
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, MagicMock())

        compact_self = registry.get_tool("compact_self")
        assert compact_self is not None
        result = asyncio.run(compact_self(session_id="s1"))

        assert result["compacted"] is False
        assert "unsupported session_type" in result["reason"]
