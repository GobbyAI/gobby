"""Tests for the compact_self MCP tool.

compact_self fires the appropriate slash command into the calling session's
CLI to trigger context compaction at workflow handoff boundaries (e.g. after
/gobby plan spawns plan-adversary). Terminal sessions go through tmux
send_keys; web_chat sessions go through the daemon-level ChatSession registry.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gobby.llm.claude_models import DoneEvent
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._terminal import (
    _CLI_COMPACT_COMMANDS,
    _send_codex_compaction_command,
    register_terminal_tools,
)
from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry
from tests._timing import drain_asyncio_tasks

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


async def _done_stream():
    yield DoneEvent(tool_calls_count=0)


def _register_compact_self(session: MagicMock, tmux_send_keys_returns: bool = True):
    registry = _TestRegistry(name="test", description="test")
    session_manager = MagicMock()
    session_manager.get.return_value = session
    session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref

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
    with (
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal.get_tmux_manager_for_context",
            return_value=tmux_manager,
        ),
        patch("gobby.mcp_proxy.tools.sessions._terminal._CODEX_INTERRUPT_SETTLE_SECONDS", 0),
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
            "interrupted": True,
            "continuation_pending": True,
        }
        assert tmux.send_keys.await_args_list == [
            call("%12", "Escape", literal=False),
            call("%12", "/compact\n", literal=True),
        ]

    def test_codex_session_interrupts_then_fires_slash_compact(self) -> None:
        session = _make_terminal_session("codex")
        registry, tmux = _register_compact_self(session)

        result = _call_compact_self(registry, tmux, session_id="s1")

        assert result == {
            "compacted": True,
            "command": "/compact",
            "cli": "codex",
            "via": "tmux",
            "interrupted": True,
            "continuation_pending": True,
        }
        assert tmux.send_keys.await_args_list == [
            call("%12", "Escape", literal=False),
            call("%12", "/compact\n", literal=True),
        ]

    @pytest.mark.asyncio
    async def test_codex_compaction_interrupt_failure_returns_false(self) -> None:
        tmux = MagicMock()
        tmux.send_keys = AsyncMock(return_value=False)

        ok, reason = await _send_codex_compaction_command(
            tmux,
            "%12",
            "/compact",
            "s1",
            settle_seconds=0,
        )

        assert ok is False
        assert reason is not None
        assert "compaction interrupt" in reason
        tmux.send_keys.assert_awaited_once_with("%12", "Escape", literal=False)

    def test_gemini_session_fires_slash_compress(self) -> None:
        session = _make_terminal_session("gemini")
        registry, tmux = _register_compact_self(session)

        result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["compacted"] is True
        assert result["command"] == "/compress"
        assert result["interrupted"] is True
        assert result["continuation_pending"] is True
        assert tmux.send_keys.await_args_list == [
            call("%12", "Escape", literal=False),
            call("%12", "/compress\n", literal=True),
        ]

    def test_qwen_session_fires_slash_compress(self) -> None:
        session = _make_terminal_session("qwen")
        registry, tmux = _register_compact_self(session)

        result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["command"] == "/compress"
        assert result["interrupted"] is True
        assert result["continuation_pending"] is True
        assert tmux.send_keys.await_args_list == [
            call("%12", "Escape", literal=False),
            call("%12", "/compress\n", literal=True),
        ]

    def test_droid_session_fires_slash_compress(self) -> None:
        session = _make_terminal_session("droid")
        registry, tmux = _register_compact_self(session)

        result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["command"] == "/compress"
        assert result["interrupted"] is True
        assert result["continuation_pending"] is True
        assert tmux.send_keys.await_args_list == [
            call("%12", "Escape", literal=False),
            call("%12", "/compress\n", literal=True),
        ]

    def test_terminal_session_marks_continuation_before_slash_command(self) -> None:
        events: list[tuple[str, str]] = []
        session = _make_terminal_session("claude")
        registry, tmux = _register_compact_self(session)

        async def send_keys(_target: str, keys: str, *, literal: bool) -> bool:
            events.append(("tmux", keys))
            return True

        def mark_pending(_db: Any, session_id: str) -> bool:
            events.append(("mark", session_id))
            return True

        tmux.send_keys.side_effect = send_keys
        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.mark_compact_self_continuation_pending",
            side_effect=mark_pending,
        ) as mock_mark:
            result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["continuation_pending"] is True
        assert events == [
            ("tmux", "Escape"),
            ("mark", "s1"),
            ("tmux", "/compact\n"),
        ]
        mock_mark.assert_called_once()

    def test_terminal_session_clears_continuation_on_slash_command_failure(self) -> None:
        session = _make_terminal_session("claude")
        registry, tmux = _register_compact_self(session)
        tmux.send_keys.side_effect = [True, False]

        with (
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.mark_compact_self_continuation_pending",
                return_value=True,
            ) as mock_mark,
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.clear_compact_self_continuation_pending",
                return_value=True,
            ) as mock_clear,
        ):
            result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["compacted"] is False
        assert "tmux send-keys failed" in result["reason"]
        mock_mark.assert_called_once()
        mock_clear.assert_called_once()


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

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.mark_compact_self_continuation_pending"
        ) as mock_mark:
            result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["compacted"] is False
        assert "no compaction command known" in result["reason"]
        assert "ubergoose" in result["reason"]
        tmux.send_keys.assert_not_called()
        mock_mark.assert_not_called()

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

    def test_tmux_send_keys_timeout_returns_compacted_false(self) -> None:
        session = _make_terminal_session("claude")
        registry, tmux = _register_compact_self(session)
        tmux.send_keys.side_effect = TimeoutError

        result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["compacted"] is False
        assert "timed out" in result["reason"]

    def test_session_ref_resolver_failure_returns_compacted_false(self) -> None:
        registry = _TestRegistry(name="test", description="test")
        session_manager = MagicMock()
        session_manager.resolve_session_reference.side_effect = TimeoutError
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, MagicMock())

        compact_self = registry.get_tool("compact_self")
        assert compact_self is not None
        result = asyncio.run(compact_self(session_id="#42"))

        assert result["compacted"] is False
        assert "failed to resolve session #42" in result["reason"]


class TestCompactSelfWebChatPath:
    def _register_web_chat(
        self,
        db_session: MagicMock,
        web_chat_registry: WebChatSessionRegistry,
        resolved_id: str = "db-id",
    ) -> _TestRegistry:
        registry = _TestRegistry(name="test", description="test")
        session_manager = MagicMock()
        session_manager.get.return_value = db_session
        session_manager.resolve_session_reference.return_value = resolved_id
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(
                registry,
                session_manager,
                MagicMock(),
                web_chat_session_registry=web_chat_registry,
            )
        return registry

    def test_web_chat_live_session_compacts_with_slash_compact(self) -> None:
        session = MagicMock()
        session.session_type = "web_chat"
        session.source = "claude"

        live_session = MagicMock()
        live_session.db_session_id = "db-id"
        live_session.conversation_id = "conv-1"
        live_session.send_message.side_effect = lambda command: _done_stream()

        web_chat_registry = WebChatSessionRegistry()
        web_chat_registry.register("conv-1", live_session)
        registry = self._register_web_chat(session, web_chat_registry)

        compact_self = registry.get_tool("compact_self")
        assert compact_self is not None
        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.mark_compact_self_continuation_pending"
        ) as mock_mark:
            result = asyncio.run(compact_self(session_id="db-id"))

        assert result == {
            "compacted": True,
            "command": "/compact",
            "via": "web_chat",
            "queued": False,
        }
        assert live_session.send_message.call_args_list == [
            call("/compact"),
            call("Continue where you last left off."),
        ]
        mock_mark.assert_not_called()

    def test_web_chat_missing_live_session_returns_compacted_false(self) -> None:
        session = MagicMock()
        session.session_type = "web_chat"
        session.source = "claude"

        web_chat_registry = WebChatSessionRegistry()
        registry = self._register_web_chat(session, web_chat_registry)

        compact_self = registry.get_tool("compact_self")
        assert compact_self is not None
        result = asyncio.run(compact_self(session_id="db-id"))

        assert result["compacted"] is False
        assert "No live web_chat session" in result["reason"]

    def test_web_chat_compaction_drains_precompact_manual_hook_output(self) -> None:
        session = MagicMock()
        session.session_type = "web_chat"
        session.source = "claude"

        live_session = MagicMock()
        live_session.db_session_id = "db-id"
        live_session.conversation_id = "conv-1"
        precompact_outputs: list[dict[str, str]] = []
        live_session._on_pre_compact = AsyncMock(
            return_value={"decision": "allow", "context": "pipeline output"}
        )

        async def compact_stream(command: str):
            if command == "/compact":
                precompact_outputs.append(await live_session._on_pre_compact({"trigger": "manual"}))
            yield DoneEvent(tool_calls_count=0)

        live_session.send_message.side_effect = compact_stream

        web_chat_registry = WebChatSessionRegistry()
        web_chat_registry.register("conv-1", live_session)
        registry = self._register_web_chat(session, web_chat_registry)

        compact_self = registry.get_tool("compact_self")
        assert compact_self is not None
        result = asyncio.run(compact_self(session_id="db-id"))

        assert result["compacted"] is True
        assert live_session.send_message.call_args_list == [
            call("/compact"),
            call("Continue where you last left off."),
        ]
        live_session._on_pre_compact.assert_awaited_once_with({"trigger": "manual"})
        assert precompact_outputs == [{"decision": "allow", "context": "pipeline output"}]

    def test_web_chat_command_matches_command_palette_compact_command(self) -> None:
        palette_source = Path("web/src/components/app/useAppCommandPalette.ts").read_text()
        assert 'sendMessage(\n      "/compact",' in palette_source

        session = MagicMock()
        session.session_type = "web_chat"
        session.source = "claude"

        live_session = MagicMock()
        live_session.db_session_id = "db-id"
        live_session.conversation_id = "conv-1"
        live_session.send_message.side_effect = lambda command: _done_stream()

        web_chat_registry = WebChatSessionRegistry()
        web_chat_registry.register("conv-1", live_session)
        registry = self._register_web_chat(session, web_chat_registry)

        compact_self = registry.get_tool("compact_self")
        assert compact_self is not None
        result = asyncio.run(compact_self(session_id="db-id"))

        assert result["command"] == "/compact"
        assert live_session.send_message.call_args_list == [
            call("/compact"),
            call("Continue where you last left off."),
        ]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_active_web_chat_session_queues_post_turn_compaction(self) -> None:
        """Active web chat turns queue compaction instead of interrupting the live turn."""
        session = MagicMock()
        session.session_type = "web_chat"
        session.source = "claude"

        live_session = MagicMock()
        live_session.db_session_id = "db-id"
        live_session.conversation_id = "conv-1"
        live_session.send_message.side_effect = lambda command: _done_stream()

        web_chat_registry = WebChatSessionRegistry()
        web_chat_registry.register("conv-1", live_session)
        registry = self._register_web_chat(session, web_chat_registry)
        compact_self = registry.get_tool("compact_self")
        assert compact_self is not None

        release = asyncio.Event()

        async def active_turn() -> None:
            await release.wait()

        active_task = asyncio.create_task(active_turn())
        web_chat_registry.track_active_task("conv-1", active_task)

        result = await compact_self(session_id="db-id")

        assert result == {
            "compacted": True,
            "command": "/compact",
            "via": "web_chat",
            "queued": True,
        }
        live_session.send_message.assert_not_called()

        release.set()
        await active_task
        await drain_asyncio_tasks()
        queued_task = web_chat_registry._queued_compaction_tasks.get("conv-1")
        assert queued_task is not None
        await queued_task
        assert live_session.send_message.call_args_list == [
            call("/compact"),
            call("Continue where you last left off."),
        ]

    def test_web_chat_session_ref_resolves_before_registry_lookup(self) -> None:
        session = MagicMock()
        session.session_type = "web_chat"
        session.source = "claude"

        live_session = MagicMock()
        live_session.db_session_id = "resolved-db-id"
        live_session.conversation_id = "conv-1"
        live_session.send_message.side_effect = lambda command: _done_stream()

        web_chat_registry = WebChatSessionRegistry()
        web_chat_registry.register("conv-1", live_session)
        registry = self._register_web_chat(
            session,
            web_chat_registry,
            resolved_id="resolved-db-id",
        )

        compact_self = registry.get_tool("compact_self")
        assert compact_self is not None
        result = asyncio.run(compact_self(session_id="#42"))

        assert result["compacted"] is True
        assert live_session.send_message.call_args_list == [
            call("/compact"),
            call("Continue where you last left off."),
        ]

    @pytest.mark.parametrize("lookup_id", ["db-id", "conv-1"])
    def test_web_chat_fallback_compacts_live_session_when_db_lookup_missing(
        self,
        lookup_id: str,
    ) -> None:
        live_session = MagicMock()
        live_session.db_session_id = "db-id"
        live_session.conversation_id = "conv-1"
        live_session.send_message.side_effect = lambda command: _done_stream()

        web_chat_registry = WebChatSessionRegistry()
        web_chat_registry.register("conv-1", live_session)

        registry = _TestRegistry(name="test", description="test")
        session_manager = MagicMock()
        session_manager.get.return_value = None
        session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(
                registry,
                session_manager,
                MagicMock(),
                web_chat_session_registry=web_chat_registry,
            )

        compact_self = registry.get_tool("compact_self")
        assert compact_self is not None
        result = asyncio.run(compact_self(session_id=lookup_id))

        assert result == {
            "compacted": True,
            "command": "/compact",
            "via": "web_chat",
            "queued": False,
        }
        assert live_session.send_message.call_args_list == [
            call("/compact"),
            call("Continue where you last left off."),
        ]

    def test_web_chat_fallback_continues_after_registry_lookup_error(self) -> None:
        live_session = MagicMock()
        live_session.db_session_id = "db-id"
        live_session.conversation_id = "conv-1"
        live_session.send_message.side_effect = lambda command: _done_stream()

        class FlakyRegistry(WebChatSessionRegistry):
            def find_session(self, session_id: str):
                if session_id == "#42":
                    raise RuntimeError("registry lookup failed")
                return super().find_session(session_id)

        web_chat_registry = FlakyRegistry()
        web_chat_registry.register("conv-1", live_session)

        registry = _TestRegistry(name="test", description="test")
        session_manager = MagicMock()
        session_manager.get.return_value = None
        session_manager.resolve_session_reference.return_value = "db-id"
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(
                registry,
                session_manager,
                MagicMock(),
                web_chat_session_registry=web_chat_registry,
            )

        compact_self = registry.get_tool("compact_self")
        assert compact_self is not None
        result = asyncio.run(compact_self(session_id="#42"))

        assert result["compacted"] is True
        assert live_session.send_message.call_args_list == [
            call("/compact"),
            call("Continue where you last left off."),
        ]

    def test_web_chat_fallback_returns_original_error_after_registry_compact_error(
        self,
    ) -> None:
        class BrokenRegistry(WebChatSessionRegistry):
            def find_session(self, session_id: str):
                return session_id, MagicMock()

            async def compact_session(self, session_id: str, command: str = "/compact"):
                raise RuntimeError("registry compact failed")

        registry = _TestRegistry(name="test", description="test")
        session_manager = MagicMock()
        session_manager.get.return_value = None
        session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(
                registry,
                session_manager,
                MagicMock(),
                web_chat_session_registry=BrokenRegistry(),
            )

        compact_self = registry.get_tool("compact_self")
        assert compact_self is not None
        result = asyncio.run(compact_self(session_id="db-id"))

        assert result == {"compacted": False, "reason": "Session db-id not found"}


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
