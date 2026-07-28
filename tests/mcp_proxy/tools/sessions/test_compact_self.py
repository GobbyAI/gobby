"""Tests for the compact_self MCP tool.

compact_self fires the appropriate slash command into the calling session's
CLI to trigger context compaction at workflow handoff boundaries (e.g. after
/gobby plan spawns plan-adversary). Terminal sessions go through tmux
send_keys; web_chat sessions go through the daemon-level ChatSession registry.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gobby.llm.claude_models import DoneEvent
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._handoff import register_handoff_tools
from gobby.mcp_proxy.tools.sessions._terminal import (
    _CLI_COMPACT_COMMANDS,
    _CODEX_INTERRUPT_SETTLE_SECONDS,
    _send_codex_compaction_command,
    _send_terminal_compaction_command,
    register_terminal_tools,
)
from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry
from gobby.sessions.compact_continuation import build_compact_self_continue_prompt
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._automation import sweep_stale_claims
from gobby.utils.session_context import session_context_for_test
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.unit


class _TestRegistry(InternalToolRegistry):
    """Registry subclass with get_tool for testing."""

    def get_tool(self, name: str) -> Callable[..., Any] | None:
        tool = self._tools.get(name)
        return tool.func if tool else None


def _make_terminal_session(source: str, tmux_pane: str | None = "%12") -> MagicMock:
    """Create a terminal session mock with optional tmux pane metadata."""
    session = MagicMock()
    session.session_type = "terminal"
    session.source = source
    session.terminal_context = (
        {"tmux_pane": tmux_pane, "tmux_socket_path": "/tmp/tmux"} if tmux_pane else {}
    )
    session.digest_markdown = "### Turn 1\nInitial handoff digest."
    session.summary_markdown = "# Cached Handoff\n\nReady."
    session.summary_source_context_hash = "source-context-hash"
    session.summary_digest_turn_count = 1
    session.transcript_path = None
    return session


def _record_background_task(scheduled: list[dict[str, Any]]) -> Callable[..., MagicMock]:
    def create_task(coro: Any, *, name: str | None = None) -> MagicMock:
        scheduled.append({"name": name})
        coro.close()
        task = MagicMock()
        task.get_name.return_value = name
        return task

    return create_task


async def _done_stream() -> AsyncIterator[DoneEvent]:
    """Yield a completed provider stream event for direct tool tests."""
    yield DoneEvent(tool_calls_count=0)


def _register_compact_self(
    session: MagicMock, tmux_send_keys_returns: bool = True
) -> tuple[_TestRegistry, MagicMock]:
    """Register compact_self with mocked session and tmux dependencies."""
    registry = _TestRegistry(name="test", description="test")
    session_manager = MagicMock()
    session_manager.get.return_value = session
    session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref

    agent_run_manager = MagicMock()
    agent_run_manager.get_by_session.return_value = None

    tmux_manager = MagicMock()
    tmux_manager.send_keys = AsyncMock(return_value=tmux_send_keys_returns)
    tmux_manager.capture_pane = AsyncMock(return_value="")

    with patch(
        "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
        return_value=agent_run_manager,
    ):
        register_terminal_tools(registry, session_manager, tmux_manager)

    return registry, tmux_manager


def _register_compact_self_with_manager(
    session_manager: MagicMock,
    tmux_send_keys_returns: bool = True,
) -> tuple[_TestRegistry, MagicMock]:
    registry = _TestRegistry(name="test", description="test")
    agent_run_manager = MagicMock()
    agent_run_manager.get_by_session.return_value = None

    tmux_manager = MagicMock()
    tmux_manager.send_keys = AsyncMock(return_value=tmux_send_keys_returns)
    tmux_manager.capture_pane = AsyncMock(return_value="")

    with patch(
        "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
        return_value=agent_run_manager,
    ):
        register_terminal_tools(registry, session_manager, tmux_manager)

    return registry, tmux_manager


def _call_compact_self(registry: _TestRegistry, tmux_manager: MagicMock, **kwargs: Any) -> Any:
    """Invoke compact_self through the registry with tmux context patched."""
    compact_self = registry.get_tool("compact_self")
    assert compact_self is not None
    caller_session_id = kwargs.pop("session_id", "s1")
    with (
        session_context_for_test(caller_session_id),
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal.get_tmux_manager_for_context",
            return_value=tmux_manager,
        ),
        patch("gobby.mcp_proxy.tools.sessions._terminal._CODEX_INTERRUPT_SETTLE_SECONDS", 0),
    ):
        return asyncio.run(compact_self(**kwargs))


def _run_direct_compact_self(
    compact_self: Callable[..., Any],
    session_id: str,
    **kwargs: Any,
) -> Any:
    """Run an async compact_self callable from a synchronous test."""
    with session_context_for_test(session_id):
        return asyncio.run(compact_self(**kwargs))


async def _await_direct_compact_self(
    compact_self: Callable[..., Any],
    session_id: str,
    **kwargs: Any,
) -> Any:
    """Await compact_self inside an existing event loop with session context."""
    with session_context_for_test(session_id):
        return await compact_self(**kwargs)


class TestCompactSelfCLIMap:
    def test_provider_command_matrix(self) -> None:
        assert _CLI_COMPACT_COMMANDS == {
            "claude": "/compact",
            "codex": "/compact",
            "grok": "/compact",
            "qwen": "/compress",
            "droid": "/compress",
        }


class TestCompactSelfTerminalPath:
    def test_schema_uses_caller_session_context(self) -> None:
        session = _make_terminal_session("codex")
        registry, _tmux = _register_compact_self(session)

        schema = registry.get_schema("compact_self")

        assert schema is not None
        input_schema = schema["inputSchema"]
        assert "session_id" not in input_schema["properties"]
        assert "session_id" not in input_schema.get("required", [])
        description = schema["description"]
        assert "interrupt the active turn before sending the slash command" in description
        assert "provider-specific compaction command" in description
        assert "rejected or cancelled compact_self tool-use message" in description
        assert "`Error: interrupted` and `Conversation interrupted`" in description
        assert "followed by `Context compacted`" in description

    def test_claude_session_fires_slash_compact_via_send_keys(self) -> None:
        session = _make_terminal_session("claude")
        registry, tmux = _register_compact_self(session)

        result = _call_compact_self(
            registry,
            tmux,
            session_id="s1",
            rule_name="auto-compact-after-task-close",
        )

        expected = {
            "compacted": True,
            "command": "/compact",
            "cli": "claude",
            "via": "tmux",
            "interrupted": True,
            "continuation_pending": True,
        }
        assert {key: result[key] for key in expected} == expected
        assert result["handoff_context_refreshed"] is True
        assert result["handoff_context_fallback"] is True
        assert result["handoff_context_background_refresh_scheduled"] is True
        assert tmux.send_keys.await_args_list == [
            call("%12", "Escape", literal=False),
            call("%12", "/compact\n", literal=True),
        ]

    def test_codex_session_interrupts_then_fires_slash_compact(self) -> None:
        session = _make_terminal_session("codex")
        registry, tmux = _register_compact_self(session)

        result = _call_compact_self(registry, tmux, session_id="s1")

        expected = {
            "compacted": True,
            "command": "/compact",
            "cli": "codex",
            "via": "tmux",
            "interrupted": True,
            "continuation_pending": True,
        }
        assert {key: result[key] for key in expected} == expected
        assert result["handoff_context_refreshed"] is True
        assert result["handoff_context_fallback"] is True
        assert result["handoff_context_background_refresh_scheduled"] is True
        assert tmux.send_keys.await_args_list == [
            call("%12", "C-c", literal=False),
            call("%12", "/compact\n", literal=True),
        ]

    @pytest.mark.parametrize(
        ("cli_source", "interrupt_key"),
        [("codex", "C-c"), ("grok", "Escape")],
    )
    @pytest.mark.asyncio
    async def test_interrupt_settles_before_marking_and_compacting(
        self,
        cli_source: str,
        interrupt_key: str,
    ) -> None:
        events: list[tuple[str, str | float]] = []
        tmux = MagicMock()
        tmux.capture_pane = AsyncMock(return_value="")

        async def send_keys(_target: str, keys: str, *, literal: bool) -> bool:
            _ = literal
            events.append(("tmux", keys))
            return True

        async def sleep(delay: float) -> None:
            events.append(("sleep", delay))

        def mark_pending() -> bool:
            events.append(("mark", "s1"))
            return True

        tmux.send_keys = AsyncMock(side_effect=send_keys)
        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal_tmux.asyncio.sleep",
            side_effect=sleep,
        ):
            (
                ok,
                reason,
                continuation_pending,
                failure_detail,
            ) = await _send_terminal_compaction_command(
                tmux,
                "%12",
                "/compact",
                "s1",
                cli_source=cli_source,
                mark_continuation_pending=mark_pending,
                clear_continuation_pending=lambda: True,
            )

        assert _CODEX_INTERRUPT_SETTLE_SECONDS == 1.0
        assert ok is True
        assert reason is None
        assert continuation_pending is True
        assert failure_detail is None
        assert events[:4] == [
            ("tmux", interrupt_key),
            ("sleep", 1.0),
            ("mark", "s1"),
            ("tmux", "/compact\n"),
        ]

    @pytest.mark.asyncio
    async def test_codex_schedules_readiness_after_marker_before_compacting(self) -> None:
        events: list[str] = []
        tmux = MagicMock()
        tmux.capture_pane = AsyncMock(return_value="before")

        async def send_keys(_target: str, keys: str, *, literal: bool) -> bool:
            _ = literal
            events.append(keys)
            return True

        def mark_pending() -> bool:
            events.append("mark")
            return True

        def schedule_readiness(before_command: str | None) -> bool:
            assert before_command == "before"
            events.append("readiness")
            return True

        tmux.send_keys = AsyncMock(side_effect=send_keys)

        result = await _send_terminal_compaction_command(
            tmux,
            "%12",
            "/compact",
            "s1",
            cli_source="codex",
            mark_continuation_pending=mark_pending,
            clear_continuation_pending=lambda: True,
            schedule_continuation_readiness=schedule_readiness,
            settle_seconds=0,
        )

        assert result == (True, None, True, None)
        assert events == ["C-c", "mark", "readiness", "/compact\n"]

    @pytest.mark.asyncio
    async def test_codex_readiness_schedule_failure_aborts_before_compacting(self) -> None:
        tmux = MagicMock()
        tmux.capture_pane = AsyncMock(return_value="before")
        tmux.send_keys = AsyncMock(return_value=True)
        clear_pending = MagicMock(return_value=True)

        result = await _send_terminal_compaction_command(
            tmux,
            "%12",
            "/compact",
            "s1",
            cli_source="codex",
            mark_continuation_pending=lambda: True,
            clear_continuation_pending=clear_pending,
            schedule_continuation_readiness=lambda _before: False,
            settle_seconds=0,
        )

        assert result == (
            False,
            "failed to schedule compact_self continuation readiness",
            False,
            None,
        )
        clear_pending.assert_called_once_with()
        assert tmux.send_keys.await_args_list == [call("%12", "C-c", literal=False)]

    @pytest.mark.asyncio
    async def test_codex_compaction_interrupt_failure_returns_false(self) -> None:
        tmux = MagicMock()
        tmux.send_keys = AsyncMock(return_value=False)
        tmux.capture_pane = AsyncMock(return_value="")

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
        tmux.send_keys.assert_awaited_once_with("%12", "C-c", literal=False)

    def test_unsupported_session_is_not_compactable(self) -> None:
        session = _make_terminal_session("unsupported")
        registry, tmux = _register_compact_self(session)

        result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["compacted"] is False
        assert "no compaction command known" in result["reason"]
        tmux.send_keys.assert_not_awaited()

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

    def test_grok_session_fires_slash_compact(self) -> None:
        session = _make_terminal_session("grok")
        registry, tmux = _register_compact_self(session)

        result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["compacted"] is True
        assert result["command"] == "/compact"
        assert result["cli"] == "grok"
        assert result["continuation_pending"] is True
        assert tmux.send_keys.await_args_list == [
            call("%12", "Escape", literal=False),
            call("%12", "/compact\n", literal=True),
        ]

    def test_current_session_backfills_tmux_context_from_same_external_id_sibling(
        self,
    ) -> None:
        current = _make_terminal_session("claude", tmux_pane=None)
        current.id = "s1"
        current.external_id = "shared-external"
        current.machine_id = "machine-1"
        current.project_id = "project-1"
        current.terminal_context = {"cwd": "/work/repos/gobby"}
        sibling = _make_terminal_session("claude", tmux_pane="%44")
        sibling.id = "sibling-session"
        sibling.external_id = "shared-external"
        sibling.machine_id = "machine-1"
        sibling.project_id = "project-1"
        sessions_by_id = {"s1": current}

        session_manager = MagicMock()
        session_manager.get.side_effect = lambda session_id: sessions_by_id.get(session_id)
        session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref
        session_manager.find_by_external_id_all_sources.return_value = [current, sibling]

        def backfill_terminal_context(
            session_id: str,
            terminal_context: dict[str, Any],
        ) -> tuple[MagicMock, bool]:
            updated = _make_terminal_session("claude", tmux_pane="%44")
            updated.id = session_id
            updated.external_id = current.external_id
            updated.machine_id = current.machine_id
            updated.project_id = current.project_id
            updated.terminal_context = {**current.terminal_context, **terminal_context}
            sessions_by_id[session_id] = updated
            return updated, True

        session_manager.backfill_terminal_context.side_effect = backfill_terminal_context
        registry, tmux = _register_compact_self_with_manager(session_manager)

        result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["compacted"] is True
        assert result["command"] == "/compact"
        assert sessions_by_id["s1"].terminal_context["cwd"] == "/work/repos/gobby"
        assert sessions_by_id["s1"].terminal_context["tmux_pane"] == "%44"
        session_manager.backfill_terminal_context.assert_called_once_with(
            "s1",
            sibling.terminal_context,
        )
        assert tmux.send_keys.await_args_list == [
            call("%44", "Escape", literal=False),
            call("%44", "/compact\n", literal=True),
        ]

    def test_terminal_session_marks_continuation_before_slash_command(self) -> None:
        events: list[tuple[str, str]] = []
        session = _make_terminal_session("claude")
        registry, tmux = _register_compact_self(session)

        async def send_keys(_target: str, keys: str, *, literal: bool) -> bool:
            events.append(("tmux", keys))
            return True

        def mark_pending(
            _db: Any,
            session_id: str,
            *,
            prompt: str,
            summary_session_id: str | None = None,
        ) -> bool:
            events.append(("mark", session_id))
            assert "Continue where you last left off." in prompt
            assert "`<!-- gobby:injected-context:begin -->`" in prompt
            assert prompt.index("use that injected context directly") < prompt.index(
                "wait_for_summary"
            )
            assert 'wait_for_summary(session_id="s1")' in prompt
            assert summary_session_id == "s1"
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

    def test_terminal_session_continuation_prompt_reloads_required_skills(self) -> None:
        session = _make_terminal_session("codex")
        registry, tmux = _register_compact_self(session)
        captured_prompts: list[str] = []

        def mark_pending(
            _db: Any,
            _session_id: str,
            *,
            prompt: str,
            summary_session_id: str | None = None,
        ) -> bool:
            captured_prompts.append(prompt)
            assert summary_session_id == "s1"
            return True

        with (
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.persist_compact_resume_required_skills",
                return_value=["python", "development-discipline"],
            ) as mock_persist,
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.mark_compact_self_continuation_pending",
                side_effect=mark_pending,
            ),
        ):
            result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["compacted"] is True
        assert result["compact_resume_required_skills"] == ["python", "development-discipline"]
        mock_persist.assert_called_once()
        assert len(captured_prompts) == 1
        assert "`<!-- gobby:injected-context:begin -->`" in captured_prompts[0]
        assert captured_prompts[0].index("use that injected context directly") < (
            captured_prompts[0].index("wait_for_summary")
        )
        assert 'wait_for_summary(session_id="s1")' in captured_prompts[0]
        assert 'call_tool("gobby-skills", "get_skill"' in captured_prompts[0]
        assert "Then continue" in captured_prompts[0]
        assert "list_mcp_servers" not in captured_prompts[0]
        assert "list_tools" not in captured_prompts[0]
        assert "get_tool_schema" not in captured_prompts[0]
        assert '"name":"python"' in captured_prompts[0]
        assert '"name":"development-discipline"' in captured_prompts[0]

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

    def test_terminal_session_clears_continuation_on_fresh_slash_rejection(self) -> None:
        session = _make_terminal_session("codex")
        registry, tmux = _register_compact_self(session)
        old_output = "old refusal\n'/compact' is disabled while a task is in progress\n$ "
        tmux.capture_pane = AsyncMock(
            side_effect=[
                old_output,
                old_output,
                old_output,
                old_output + "/compact\n'/compact' is disabled while a task is in progress\n",
            ]
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.mark_compact_self_continuation_pending",
                return_value=True,
            ) as mock_mark,
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.clear_compact_self_continuation_pending",
                return_value=True,
            ) as mock_clear,
            patch("gobby.mcp_proxy.tools.sessions._terminal.asyncio.sleep", new=AsyncMock()),
        ):
            result = _call_compact_self(registry, tmux, session_id="s1")

        assert result == {
            "compacted": False,
            "reason": "'/compact' is disabled while a task is in progress",
            "error_code": "compaction_command_rejected",
            "rejected_command": "/compact",
            "rejection_message": "'/compact' is disabled while a task is in progress",
        }
        assert tmux.send_keys.await_args_list == [
            call("%12", "C-c", literal=False),
            call("%12", "/compact\n", literal=True),
        ]
        assert tmux.capture_pane.await_args_list == [
            call("%12", lines=1),
            call("%12", lines=30),
            call("%12", lines=100),
            call("%12", lines=30),
        ]
        mock_mark.assert_called_once()
        mock_clear.assert_called_once()

    def test_terminal_session_reuses_fresh_cached_handoff_before_compacting(self) -> None:
        events: list[str] = []
        session = _make_terminal_session("codex")
        session.id = "s1"
        session.title = "Coordinator"
        session.status = "active"
        session.digest_markdown = "### Turn 4\nFresh transcript digest for #15040."
        session.transcript_path = None
        session.summary_markdown = "# Fresh Compact Handoff\n\nReady."
        session.summary_source_context_hash = "existing-source-hash"
        session.summary_digest_turn_count = 1

        registry = _TestRegistry(name="test", description="test")
        session_manager = MagicMock()
        session_manager.get.return_value = session
        session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref

        def update_status(session_id: str, status: str) -> None:
            assert session_id == "s1"
            events.append(f"status:{status}")
            session.status = status

        session_manager.update_status.side_effect = update_status
        db = MagicMock()
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        tmux = MagicMock()
        tmux.capture_pane = AsyncMock(return_value="")

        async def send_keys(_target: str, keys: str, *, literal: bool) -> bool:
            events.append(f"tmux:{keys}")
            return True

        tmux.send_keys = AsyncMock(side_effect=send_keys)

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, db, llm_service=MagicMock())
            register_handoff_tools(registry, session_manager)

        compact_self = registry.get_tool("compact_self")
        get_handoff_context = registry.get_tool("get_handoff_context")
        assert compact_self is not None
        assert get_handoff_context is not None

        with (
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.get_tmux_manager_for_context",
                return_value=tmux,
            ),
            patch("gobby.mcp_proxy.tools.sessions._terminal._CODEX_INTERRUPT_SETTLE_SECONDS", 0),
            patch(
                "gobby.sessions.summarize.generate_session_summaries",
                new_callable=AsyncMock,
            ) as mock_refresh,
            patch(
                "gobby.mcp_proxy.tools.sessions._summary_metadata.compact_summary_metadata_matches",
                new=AsyncMock(return_value=True),
            ) as mock_metadata_matches,
            session_context_for_test("s1"),
        ):
            result = asyncio.run(compact_self())
            handoff = get_handoff_context(session_id="s1")

        assert result["compacted"] is True
        assert "handoff_context_refreshed" not in result
        assert "handoff_context_fallback" not in result
        assert "handoff_context_background_refresh_scheduled" not in result
        assert events == ["status:handoff_ready", "tmux:C-c", "tmux:/compact\n"]
        mock_refresh.assert_not_called()
        mock_metadata_matches.assert_awaited_once()
        assert handoff["context_type"] == "summary_markdown"
        assert handoff["context"] == "# Fresh Compact Handoff\n\nReady."

    def test_terminal_session_compacts_with_digest_fallback_without_waiting_for_refresh(
        self,
    ) -> None:
        events: list[str] = []
        scheduled: list[dict[str, Any]] = []
        session = _make_terminal_session("codex")
        session.id = "s1"
        session.title = "Coordinator"
        session.status = "active"
        session.digest_markdown = "### Turn 8\nLatest coordinator state for #15156."
        session.transcript_path = None
        session.summary_markdown = "stale pre-compaction summary"
        session.summary_source_context_hash = None
        session.summary_digest_turn_count = None
        persist_calls: list[dict[str, Any]] = []

        registry = _TestRegistry(name="test", description="test")

        class RevisionAwareSessionManager:
            def get(self, session_id: str) -> MagicMock | None:
                return session if session_id == "s1" else None

            def resolve_session_reference(
                self,
                ref: str,
                project_id: str | None = None,
            ) -> str:
                return ref

            def persist_summary_state(
                self,
                session_id: str,
                *,
                summary_markdown: str,
                generation_mode: str,
                source_context_hash: str | None = None,
                source_digest_turn_count: int | None = None,
                metadata_json: dict[str, Any] | None = None,
            ) -> MagicMock:
                assert session_id == "s1"
                events.append("persist_summary_state")
                persist_calls.append(
                    {
                        "generation_mode": generation_mode,
                        "source_context_hash": source_context_hash,
                        "source_digest_turn_count": source_digest_turn_count,
                        "metadata_json": metadata_json or {},
                    }
                )
                session.summary_markdown = summary_markdown
                return session

            def update_summary(self, session_id: str, *, summary_markdown: str) -> None:
                assert session_id == "s1"
                events.append("update_summary")
                session.summary_markdown = summary_markdown

            def update_status(self, session_id: str, status: str) -> None:
                assert session_id == "s1"
                events.append(f"status:{status}")
                session.status = status

        session_manager = RevisionAwareSessionManager()
        db = MagicMock()
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        tmux = MagicMock()
        tmux.capture_pane = AsyncMock(return_value="")

        async def send_keys(_target: str, keys: str, *, literal: bool) -> bool:
            events.append(f"tmux:{keys}")
            return True

        tmux.send_keys = AsyncMock(side_effect=send_keys)

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, db, llm_service=MagicMock())
            register_handoff_tools(registry, session_manager)

        compact_self = registry.get_tool("compact_self")
        get_handoff_context = registry.get_tool("get_handoff_context")
        assert compact_self is not None
        assert get_handoff_context is not None

        with (
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.get_tmux_manager_for_context",
                return_value=tmux,
            ),
            patch("gobby.mcp_proxy.tools.sessions._terminal._CODEX_INTERRUPT_SETTLE_SECONDS", 0),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.asyncio.create_task",
                side_effect=_record_background_task(scheduled),
            ),
            patch(
                "gobby.sessions.summarize.generate_session_summaries",
                new_callable=AsyncMock,
            ) as mock_refresh,
            patch(
                "gobby.mcp_proxy.tools.sessions._summary_metadata.compact_summary_metadata_matches",
                new=AsyncMock(return_value=False),
            ) as mock_metadata_matches,
            session_context_for_test("s1"),
        ):
            result = asyncio.run(compact_self())
            handoff = get_handoff_context(session_id="s1")

        assert result["compacted"] is True
        assert result["handoff_context_refreshed"] is True
        assert result["handoff_context_fallback"] is True
        assert result["handoff_context_background_refresh_scheduled"] is True
        assert "handoff_context_refresh_timed_out" not in result
        assert events == [
            "persist_summary_state",
            "status:handoff_ready",
            "tmux:C-c",
            "tmux:/compact\n",
        ]
        assert persist_calls == [
            {
                "generation_mode": "digest_fallback",
                "source_context_hash": None,
                "source_digest_turn_count": 1,
                "metadata_json": {
                    "reason": "summary metadata stale or missing",
                    "source": "compact_self",
                },
            }
        ]
        assert scheduled == [{"name": "compact-handoff-refresh-s1"}]
        mock_refresh.assert_not_called()
        mock_metadata_matches.assert_awaited_once()
        assert "Latest coordinator state for #15156." in handoff["context"]
        assert "stale pre-compaction" not in handoff["context"]

    def test_delayed_archival_refresh_preserves_resumed_session_claim(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, Any],
    ) -> None:
        session_manager = SessionManager(temp_db)
        session_id = session_manager.register_session(
            external_id="compact-claim-regression",
            machine_id="machine-test",
            source="codex",
            project_id=sample_project["id"],
            title="Compact claim regression",
            terminal_context={"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
        )
        assert session_id
        temp_db.execute(
            """
            UPDATE sessions
               SET digest_markdown = %s,
                   summary_markdown = %s,
                   summary_source_context_hash = NULL,
                   summary_digest_turn_count = NULL
             WHERE id = %s
            """,
            ("### Turn 1\nClaimed task is still running.", "stale summary", session_id),
        )

        task_manager = LocalTaskManager(temp_db)
        task = task_manager.create_task(
            project_id=sample_project["id"],
            title="Survive delayed compact summary",
            category="test",
            task_type="task",
            validation_criteria="Test task completion is observable.",
        )
        task_manager.claim_task(task.id, session_id)

        registry = _TestRegistry(name="test", description="test")
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None
        tmux = MagicMock()
        tmux.capture_pane = AsyncMock(return_value="")
        tmux.send_keys = AsyncMock(return_value=True)
        pending_refreshes: list[Any] = []
        summary_handoff_flags: list[bool] = []

        def retain_background_refresh(coro: Any, *, name: str | None = None) -> MagicMock:
            pending_refreshes.append(coro)
            task_handle = MagicMock()
            task_handle.get_name.return_value = name
            return task_handle

        async def complete_delayed_summary(**kwargs: Any) -> dict[str, Any]:
            set_handoff_ready = kwargs["set_handoff_ready"]
            summary_handoff_flags.append(set_handoff_ready)
            if set_handoff_ready:
                session_manager.update_session_status(session_id, "handoff_ready")
            return {"success": True}

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, temp_db, llm_service=MagicMock())

        compact_self = registry.get_tool("compact_self")
        assert compact_self is not None

        with (
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.get_tmux_manager_for_context",
                return_value=tmux,
            ),
            patch("gobby.mcp_proxy.tools.sessions._terminal._CODEX_INTERRUPT_SETTLE_SECONDS", 0),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.asyncio.create_task",
                side_effect=retain_background_refresh,
            ),
            patch(
                "gobby.sessions.summarize.generate_session_summaries",
                side_effect=complete_delayed_summary,
            ),
            patch(
                "gobby.mcp_proxy.tools.sessions._summary_metadata.compact_summary_metadata_matches",
                new=AsyncMock(return_value=False),
            ),
            session_context_for_test(session_id),
        ):
            result = asyncio.run(compact_self())
            assert session_manager.get(session_id).status == "handoff_ready"

            assert session_manager.update_session_status(
                session_id,
                "active",
                activity_confirmed=True,
            )
            assert len(pending_refreshes) == 1
            asyncio.run(pending_refreshes.pop())

        sweep_stale_claims(temp_db, project_id=sample_project["id"])
        session = session_manager.get(session_id)
        claimed_task = task_manager.get_task(task.id)

        assert result["compacted"] is True
        assert summary_handoff_flags == [False]
        assert session is not None
        assert session.status == "active"
        assert claimed_task is not None
        assert claimed_task.claimed_by_session_id == session_id

    def test_terminal_session_uses_transcript_tail_fallback_when_digest_missing(
        self,
        tmp_path: Path,
    ) -> None:
        events: list[str] = []
        scheduled: list[dict[str, Any]] = []
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text("\n".join(f"transcript line {index}" for index in range(90)))

        session = _make_terminal_session("codex")
        session.id = "s1"
        session.title = "Coordinator"
        session.status = "active"
        session.digest_markdown = None
        session.transcript_path = str(transcript_path)
        session.summary_markdown = None
        persist_calls: list[dict[str, Any]] = []

        registry = _TestRegistry(name="test", description="test")
        session_manager = MagicMock()
        session_manager.get.return_value = session
        session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref

        def persist_summary_state(
            session_id: str,
            *,
            summary_markdown: str,
            generation_mode: str,
            source_context_hash: str | None = None,
            source_digest_turn_count: int | None = None,
            metadata_json: dict[str, Any] | None = None,
        ) -> MagicMock:
            assert session_id == "s1"
            events.append("persist_summary_state")
            persist_calls.append(
                {
                    "summary_markdown": summary_markdown,
                    "generation_mode": generation_mode,
                    "source_context_hash": source_context_hash,
                    "source_digest_turn_count": source_digest_turn_count,
                    "metadata_json": metadata_json or {},
                }
            )
            session.summary_markdown = summary_markdown
            return session

        def update_status(session_id: str, status: str) -> None:
            assert session_id == "s1"
            events.append(f"status:{status}")
            session.status = status

        session_manager.persist_summary_state.side_effect = persist_summary_state
        session_manager.update_status.side_effect = update_status
        db = MagicMock()
        agent_run_manager = MagicMock()
        agent_run_manager.get_by_session.return_value = None

        tmux = MagicMock()
        tmux.capture_pane = AsyncMock(return_value="")

        async def send_keys(_target: str, keys: str, *, literal: bool) -> bool:
            events.append(f"tmux:{keys}")
            return True

        tmux.send_keys = AsyncMock(side_effect=send_keys)

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
            return_value=agent_run_manager,
        ):
            register_terminal_tools(registry, session_manager, db, llm_service=MagicMock())
            register_handoff_tools(registry, session_manager)

        compact_self = registry.get_tool("compact_self")
        get_handoff_context = registry.get_tool("get_handoff_context")
        assert compact_self is not None
        assert get_handoff_context is not None

        with (
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.get_tmux_manager_for_context",
                return_value=tmux,
            ),
            patch("gobby.mcp_proxy.tools.sessions._terminal._CODEX_INTERRUPT_SETTLE_SECONDS", 0),
            patch(
                "gobby.mcp_proxy.tools.sessions._terminal.asyncio.create_task",
                side_effect=_record_background_task(scheduled),
            ),
            patch(
                "gobby.sessions.summarize.generate_session_summaries",
                new_callable=AsyncMock,
            ) as mock_refresh,
            session_context_for_test("s1"),
        ):
            result = asyncio.run(compact_self())
            handoff = get_handoff_context(session_id="s1")

        assert result["compacted"] is True
        assert result["handoff_context_refreshed"] is True
        assert result["handoff_context_fallback"] is True
        assert result["handoff_context_background_refresh_scheduled"] is True
        assert events == [
            "persist_summary_state",
            "status:handoff_ready",
            "tmux:C-c",
            "tmux:/compact\n",
        ]
        assert scheduled == [{"name": "compact-handoff-refresh-s1"}]
        mock_refresh.assert_not_called()
        assert persist_calls[0]["generation_mode"] == "digest_fallback"
        assert persist_calls[0]["source_context_hash"] is None
        assert persist_calls[0]["source_digest_turn_count"] == 0
        assert persist_calls[0]["metadata_json"] == {
            "reason": "digest missing",
            "source": "compact_self",
        }
        assert "transcript line 89" in handoff["context"]
        assert "transcript line 0" not in handoff["context"]


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
        with session_context_for_test("missing"):
            result = asyncio.run(compact_self())

        assert result["compacted"] is False
        assert "not found" in result["reason"]

    def test_missing_session_context_returns_compacted_false(self) -> None:
        session = _make_terminal_session("codex")
        registry, _tmux = _register_compact_self(session)

        compact_self = registry.get_tool("compact_self")
        assert compact_self is not None
        result = asyncio.run(compact_self())

        assert result["compacted"] is False
        assert "SessionContext" in result["reason"]

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

    def test_deleted_session_remains_terminal(self) -> None:
        session = _make_terminal_session("codex")
        session.status = "deleted"
        registry, tmux = _register_compact_self(session)

        result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["compacted"] is False
        assert result["error_code"] == "session_deleted"
        tmux.capture_pane.assert_not_awaited()

    def test_non_live_tmux_target_blocks_before_handoff_mutation(self) -> None:
        session = _make_terminal_session("codex")
        session.status = "active"
        registry, tmux = _register_compact_self(session)
        tmux.capture_pane.return_value = None

        result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["compacted"] is False
        assert result["error_code"] == "tmux_target_not_live"

    def test_expired_session_is_revived_through_explicit_activity_path(self) -> None:
        session = _make_terminal_session("codex")
        session.status = "expired"
        registry, tmux = _register_compact_self(session)
        activity = MagicMock(success=True, session=session)

        with patch(
            "gobby.mcp_proxy.tools.sessions._terminal.reconcile_compact_session_activity",
            return_value=activity,
        ) as reconcile:
            result = _call_compact_self(registry, tmux, session_id="s1")

        assert result["compacted"] is True
        reconcile.assert_called_once()

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
        with session_context_for_test("#42"):
            result = asyncio.run(compact_self())

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
            result = _run_direct_compact_self(compact_self, "db-id")

        assert result == {
            "compacted": True,
            "command": "/compact",
            "via": "web_chat",
            "queued": False,
        }
        assert live_session.send_message.call_args_list == [
            call("/compact"),
            call(build_compact_self_continue_prompt(None, summary_session_id="db-id")),
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
        result = _run_direct_compact_self(compact_self, "db-id")

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

        async def compact_stream(command: str) -> AsyncIterator[DoneEvent]:
            if command == "/compact":
                precompact_outputs.append(await live_session._on_pre_compact({"trigger": "manual"}))
            yield DoneEvent(tool_calls_count=0)

        live_session.send_message.side_effect = compact_stream

        web_chat_registry = WebChatSessionRegistry()
        web_chat_registry.register("conv-1", live_session)
        registry = self._register_web_chat(session, web_chat_registry)

        compact_self = registry.get_tool("compact_self")
        assert compact_self is not None
        result = _run_direct_compact_self(compact_self, "db-id")

        assert result["compacted"] is True
        assert live_session.send_message.call_args_list == [
            call("/compact"),
            call(build_compact_self_continue_prompt(None, summary_session_id="db-id")),
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
        result = _run_direct_compact_self(compact_self, "db-id")

        assert result["command"] == "/compact"
        assert live_session.send_message.call_args_list == [
            call("/compact"),
            call(build_compact_self_continue_prompt(None, summary_session_id="db-id")),
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

        result = await _await_direct_compact_self(compact_self, "db-id")

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
            call(build_compact_self_continue_prompt(None, summary_session_id="db-id")),
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
        result = _run_direct_compact_self(compact_self, "#42")

        assert result["compacted"] is True
        assert live_session.send_message.call_args_list == [
            call("/compact"),
            call(build_compact_self_continue_prompt(None, summary_session_id="resolved-db-id")),
        ]

    @pytest.mark.parametrize("lookup_id", ["db-id", "conv-1"])
    def test_web_chat_fallback_compacts_live_session_when_db_lookup_missing(
        self,
        lookup_id: str,
    ) -> None:
        """Missing DB rows can still compact a live web-chat session by either id."""
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
        result = _run_direct_compact_self(compact_self, lookup_id)

        assert result == {
            "compacted": True,
            "command": "/compact",
            "via": "web_chat",
            "queued": False,
        }
        assert live_session.send_message.call_args_list == [
            call("/compact"),
            call(build_compact_self_continue_prompt(None, summary_session_id="db-id")),
        ]

    def test_web_chat_fallback_continues_after_registry_lookup_error(self) -> None:
        """A mocked live-session lookup RuntimeError falls through to resolved DB id."""
        live_session = MagicMock()
        live_session.db_session_id = "db-id"
        live_session.conversation_id = "conv-1"
        live_session.send_message.side_effect = lambda command: _done_stream()

        class FlakyRegistry(WebChatSessionRegistry):
            def find_session(
                self, session_id: str
            ) -> tuple[str | None, ChatSessionProtocol | None]:
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
        result = _run_direct_compact_self(compact_self, "#42")

        assert result["compacted"] is True
        assert live_session.send_message.call_args_list == [
            call("/compact"),
            call(build_compact_self_continue_prompt(None, summary_session_id="db-id")),
        ]

    def test_web_chat_fallback_returns_original_error_after_registry_compact_error(
        self,
    ) -> None:
        """Fallback compaction errors preserve the original DB lookup failure response."""

        class BrokenRegistry(WebChatSessionRegistry):
            def find_session(
                self, session_id: str
            ) -> tuple[str | None, ChatSessionProtocol | None]:
                return session_id, MagicMock()

            async def compact_session(
                self, session_id: str, command: str = "/compact"
            ) -> dict[str, Any]:
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
        result = _run_direct_compact_self(compact_self, "db-id")

        assert result == {"compacted": False, "reason": "Session db-id not found"}

    def test_web_chat_fallback_continues_after_registry_compact_error(self) -> None:
        """A failed original-id fallback compact still tries the resolved DB id."""

        class PartiallyBrokenRegistry(WebChatSessionRegistry):
            def __init__(self) -> None:
                super().__init__()
                self.compacted_session_ids: list[str] = []

            def find_session(
                self, session_id: str
            ) -> tuple[str | None, ChatSessionProtocol | None]:
                return session_id, MagicMock()

            async def compact_session(
                self, session_id: str, command: str = "/compact"
            ) -> dict[str, Any]:
                self.compacted_session_ids.append(session_id)
                if session_id == "#42":
                    raise RuntimeError("registry compact failed")
                return {"compacted": True, "session_id": session_id}

        web_chat_registry = PartiallyBrokenRegistry()
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
        result = _run_direct_compact_self(compact_self, "#42")

        assert result == {"compacted": True, "session_id": "db-id"}
        assert web_chat_registry.compacted_session_ids == ["#42", "db-id"]


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
        result = _run_direct_compact_self(compact_self, "s1")

        assert result["compacted"] is False
        assert "unsupported session_type" in result["reason"]
