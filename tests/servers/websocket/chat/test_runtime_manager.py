"""Tests for shared web-chat runtime manager and provider backends."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gobby.adapters.gemini_acp_client import StreamEvent
from gobby.agents.sandbox import SandboxConfig
from gobby.config.app import DaemonConfig
from gobby.llm.claude_models import DoneEvent, TextChunk, ToolCallEvent, ToolResultEvent
from gobby.servers.chat_session import ChatSession
from gobby.servers.websocket.chat.backends import (
    CodexManagedChatSession,
    CodexWebChatBackend,
    GeminiManagedChatSession,
    GeminiWebChatBackend,
    QwenManagedChatSession,
    QwenWebChatBackend,
)
from gobby.servers.websocket.chat.runtime_manager import WebChatRuntimeManager
from gobby.sessions.transcripts.base import ParsedMessage

pytestmark = pytest.mark.unit

PYTHON_SKILL_DIRECTIVE = 'Call get_skill(name="python") on gobby-skills, then continue.'
CODE_INDEX_SKILL_DIRECTIVE = 'Call get_skill(name="code-index") on gobby-skills, then continue.'
TASK_TRANSITIONS_SKILL_DIRECTIVE = (
    'Call get_skill(name="task-transitions") on gobby-skills, then continue.'
)


def _async_stream(*items: Any):
    async def _gen():
        for item in items:
            yield item

    return _gen()


class TestWebChatRuntimeManager:
    def test_create_session_routes_by_provider(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None)

        claude_session = manager.create_session(provider="claude", conversation_id="conv-1")
        gemini_session = manager.create_session(provider="gemini", conversation_id="conv-2")
        qwen_session = manager.create_session(provider="qwen", conversation_id="conv-3")
        codex_session = manager.create_session(provider="codex", conversation_id="conv-4")

        assert isinstance(claude_session, ChatSession)
        assert isinstance(gemini_session, GeminiManagedChatSession)
        assert isinstance(qwen_session, QwenManagedChatSession)
        assert isinstance(codex_session, CodexManagedChatSession)

    def test_health_snapshot_contains_droid(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None)

        health = manager.health_snapshot()

        assert "droid" in health
        assert health["droid"]["provider"] == "droid"

    def test_create_session_routes_droid_to_managed_backend(self) -> None:
        from gobby.servers.websocket.chat.backends import DroidManagedChatSession

        manager = WebChatRuntimeManager(codex_client=None)

        droid_session = manager.create_session(provider="droid", conversation_id="conv-droid")

        assert isinstance(droid_session, DroidManagedChatSession)
        assert droid_session.provider == "droid"
        assert droid_session._provider_label() == "droid"

    def test_create_session_applies_codex_transcript_retry_config(self) -> None:
        manager = WebChatRuntimeManager(
            codex_client=None,
            codex_transcript_retry_attempts=2,
            codex_transcript_retry_delay_seconds=0.25,
        )

        codex_session = manager.create_session(provider="codex", conversation_id="conv-3")

        assert isinstance(codex_session, CodexManagedChatSession)
        assert codex_session._transcript_retry_attempts == 2
        assert codex_session._transcript_retry_delay_seconds == 0.25

    def test_manager_uses_daemon_owned_web_chat_sandbox_defaults(self) -> None:
        manager = WebChatRuntimeManager(
            codex_client=None,
            daemon_config=DaemonConfig(
                web_chat_sandbox={
                    "enabled": False,
                    "extra_read_paths": ["/tmp/web-read"],
                    "extra_write_paths": ["/tmp/web-write"],
                },
            ),
        )

        assert manager._claude_backend._sandbox_config is not None
        assert manager._claude_backend._sandbox_config.enabled is False
        assert manager._codex_backend._sandbox_config is not None
        assert manager._codex_backend._sandbox_config.enabled is False
        assert manager._gemini_backend._sandbox_config is not None
        assert manager._gemini_backend._sandbox_config.extra_read_paths == ["/tmp/web-read"]
        assert manager._qwen_backend._sandbox_config is not None
        assert manager._qwen_backend._sandbox_config.extra_write_paths == ["/tmp/web-write"]
        assert manager._droid_backend._sandbox_config is not None
        assert manager._droid_backend._sandbox_config.enabled is False
        assert manager._droid_backend._sandbox_config.extra_read_paths == ["/tmp/web-read"]

    def test_manager_defaults_web_chat_sandbox_to_enabled(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None, daemon_config=DaemonConfig())

        assert manager.sandbox_config.enabled is True
        assert manager.sandbox_policy_hash

    @pytest.mark.asyncio
    async def test_background_start_skips_acp_backends(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None)
        manager._codex_backend.start = AsyncMock()
        manager._gemini_backend.start = AsyncMock()
        manager._qwen_backend.start = AsyncMock()
        manager._droid_backend.start = AsyncMock()

        await manager.start(background=True)

        manager._codex_backend.start.assert_awaited_once_with(background=True)
        manager._droid_backend.start.assert_awaited_once_with(background=True)
        manager._gemini_backend.start.assert_not_awaited()
        manager._qwen_backend.start.assert_not_awaited()


class TestGeminiBackend:
    def test_backend_does_not_build_full_process_sandboxed_acp_client(self) -> None:
        with patch.object(GeminiWebChatBackend, "acp_client_cls") as mock_client:
            GeminiWebChatBackend(sandbox_config=SandboxConfig(enabled=True, allow_network=False))

        # Provider/display_name now come from class attributes on GeminiACPClient;
        # the backend should not pass any sandbox-leaking process args.
        assert mock_client.call_args is not None
        kwargs = mock_client.call_args.kwargs
        assert "extra_args" not in kwargs
        assert "env_overrides" not in kwargs

    @pytest.mark.asyncio
    async def test_start_marks_backend_unavailable_on_error(self) -> None:
        client = MagicMock()
        client.is_started = False
        client.start = AsyncMock(side_effect=RuntimeError("boom"))

        backend = GeminiWebChatBackend(client=client)
        await backend.start()

        health = backend.health()
        assert health.available is False
        assert health.startup_error == "boom"

    @pytest.mark.asyncio
    async def test_start_reports_explicit_timeout_message(self) -> None:
        client = MagicMock()
        client.is_started = False
        client.start = AsyncMock(side_effect=TimeoutError())

        backend = GeminiWebChatBackend(client=client)
        await backend.start()

        health = backend.health()
        assert health.available is False
        assert health.startup_error == "Timed out starting Gemini ACP backend after 15.0s"

    @pytest.mark.asyncio
    async def test_managed_session_translates_stream_events(self) -> None:
        backend = MagicMock()
        backend.attach_session = AsyncMock()
        backend.send_message = MagicMock(
            return_value=_async_stream(
                StreamEvent(event_type="content_delta", data={"content": "Hello "}),
                StreamEvent(event_type="content_delta", data={"content": "Gemini"}),
                StreamEvent(event_type="result", data={}),
            )
        )
        session = GeminiManagedChatSession(conversation_id="conv-gem", _backend=backend)
        session._connected = True
        session._model = "gemini-ctx"
        session._context_window_overrides = {"gemini-ctx": 123_000}
        session.sdk_session_id = "sess-1"

        events = [event async for event in session.send_message("hi")]

        assert [e.content for e in events if isinstance(e, TextChunk)] == ["Hello ", "Gemini"]
        assert isinstance(events[-1], DoneEvent)
        assert events[-1].context_window == 123_000

    @pytest.mark.asyncio
    async def test_managed_session_defers_tool_lifecycle_context_to_next_turn(self) -> None:
        backend = MagicMock()
        backend.attach_session = AsyncMock()
        backend.send_message = MagicMock(
            side_effect=[
                _async_stream(
                    StreamEvent(
                        event_type="tool_call",
                        data={
                            "tool_name": "Write",
                            "tool_input": {"file_path": "/tmp/example.py"},
                            "call_id": "call-1",
                        },
                    ),
                    StreamEvent(
                        event_type="tool_result",
                        data={"call_id": "call-1", "success": True, "result": "ok"},
                    ),
                    StreamEvent(event_type="result", data={}),
                ),
                _async_stream(StreamEvent(event_type="result", data={})),
            ]
        )
        session = GeminiManagedChatSession(conversation_id="conv-gem", _backend=backend)
        session._connected = True
        session.sdk_session_id = "sess-1"
        session._on_pre_tool = AsyncMock(return_value={"context": PYTHON_SKILL_DIRECTIVE})
        session._on_post_tool = AsyncMock(
            return_value={"context": TASK_TRANSITIONS_SKILL_DIRECTIVE}
        )

        first_events = [event async for event in session.send_message("first")]
        second_events = [event async for event in session.send_message("second")]

        assert any(isinstance(event, ToolCallEvent) for event in first_events)
        assert any(isinstance(event, ToolResultEvent) for event in first_events)
        session._on_pre_tool.assert_awaited_once_with(
            {"tool_name": "Write", "tool_input": {"file_path": "/tmp/example.py"}}
        )
        session._on_post_tool.assert_awaited_once_with(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/example.py"},
                "tool_response": "ok",
            }
        )
        assert isinstance(second_events[-1], DoneEvent)
        assert PYTHON_SKILL_DIRECTIVE in backend.send_message.call_args_list[1].args[1]
        assert TASK_TRANSITIONS_SKILL_DIRECTIVE in backend.send_message.call_args_list[1].args[1]

    def test_plan_mode_context_teaches_gcode(self) -> None:
        session = GeminiManagedChatSession(conversation_id="conv-gem", _backend=MagicMock())
        session.chat_mode = "plan"

        context = session._pop_plan_mode_context()

        assert context is not None
        assert "gcode outline/search/symbol" in context
        assert "Bash/exec_command" in context


class TestQwenBackend:
    def test_backend_does_not_build_full_process_sandboxed_acp_client(self) -> None:
        with patch.object(QwenWebChatBackend, "acp_client_cls") as mock_client:
            QwenWebChatBackend(sandbox_config=SandboxConfig(enabled=True, allow_network=False))

        # cli_name / display_name / prompt_timeout_env are now class attributes
        # on QwenACPClient; the backend should not pass sandbox-leaking process args.
        assert mock_client.call_args is not None
        kwargs = mock_client.call_args.kwargs
        assert "extra_args" not in kwargs
        assert "env_overrides" not in kwargs

    def test_qwen_inherits_gemini_plan_mode_gcode_context(self) -> None:
        session = QwenManagedChatSession(conversation_id="conv-qwen", _backend=MagicMock())
        session.chat_mode = "plan"

        context = session._pop_plan_mode_context()

        assert context is not None
        assert "gcode outline/search/symbol" in context

    def test_managed_session_logs_upstream_error_context(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        session = QwenManagedChatSession(conversation_id="conv-qwen", _backend=MagicMock())
        session.db_session_id = "db-qwen"
        session.sdk_session_id = "sdk-qwen"
        session._model = "qwen3-coder"

        with caplog.at_level("WARNING"):
            event = session._translate_event(
                StreamEvent(
                    event_type="error",
                    data={"message": "Internal error", "code": "upstream_internal"},
                )
            )

        assert isinstance(event, TextChunk)
        assert event.content == "Error: Internal error"
        assert any("Managed qwen upstream error" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_managed_session_done_event_includes_context_window(self) -> None:
        backend = MagicMock()
        backend.attach_session = AsyncMock()
        backend.send_message = MagicMock(
            return_value=_async_stream(StreamEvent(event_type="result", data={}))
        )
        session = QwenManagedChatSession(conversation_id="conv-qwen", _backend=backend)
        session._connected = True
        session.sdk_session_id = "sess-qwen"
        session._model = "qwen3-coder"
        session._context_window_overrides = {"qwen3-coder": 262_144}

        events = [event async for event in session.send_message("hi")]

        assert isinstance(events[-1], DoneEvent)
        assert events[-1].context_window == 262_144

    def test_managed_session_translates_structured_tool_events(self) -> None:
        session = QwenManagedChatSession(conversation_id="conv-qwen", _backend=MagicMock())

        tool_call = session._translate_event(
            StreamEvent(
                event_type="tool_call",
                data={
                    "tool_name": "Write",
                    "tool_input": {"file_path": "/tmp/example.py"},
                    "call_id": "call-1",
                },
            )
        )
        tool_result = session._translate_event(
            StreamEvent(
                event_type="tool_result",
                data={"call_id": "call-1", "success": True, "result": "ok"},
            )
        )

        assert isinstance(tool_call, ToolCallEvent)
        assert tool_call.tool_call_id == "call-1"
        assert tool_call.tool_name == "Write"
        assert tool_call.arguments == {"file_path": "/tmp/example.py"}
        assert isinstance(tool_result, ToolResultEvent)
        assert tool_result.tool_call_id == "call-1"
        assert tool_result.success is True
        assert tool_result.result == "ok"

    @pytest.mark.asyncio
    async def test_attach_session_warms_local_openai_models(self) -> None:
        client = MagicMock()
        client.is_started = True
        client.create_session = AsyncMock(return_value={"sessionId": "sess-qwen"})

        backend = QwenWebChatBackend(client=client)
        backend._health.available = True
        backend.start = AsyncMock()

        session = QwenManagedChatSession(conversation_id="conv-qwen", _backend=backend)
        session.project_path = "/tmp/project"
        session._model = "qwen3.6-35b-a3b-q8-local(openai)"

        with patch(
            "gobby.servers.websocket.chat.backends.qwen.ensure_qwen_local_openai_model_ready",
            new=AsyncMock(),
        ) as mock_warmup:
            await backend.attach_session(session)

        mock_warmup.assert_awaited_once_with(
            "qwen3.6-35b-a3b-q8-local(openai)",
            project_path="/tmp/project",
        )
        assert mock_warmup.await_count == 1
        assert mock_warmup.await_args is not None
        resolved_project_path = str(Path(session.project_path).resolve())
        client.create_session.assert_awaited_once_with(
            model="qwen3.6-35b-a3b-q8-local(openai)",
            cwd=resolved_project_path,
            reasoning_effort=None,
        )
        assert client.create_session.await_count == 1
        assert client.create_session.await_args is not None


async def _collect_codex_backend_events(
    notifications: list[tuple[str, dict[str, Any]]],
    *,
    transcript_path: Path | None = None,
    transcript_lines: list[str] | None = None,
) -> tuple[list[Any], CodexManagedChatSession]:
    handlers: dict[str, list[Any]] = {}

    def add_handler(method: str, handler: Any) -> None:
        handlers.setdefault(method, []).append(handler)

    async def start_turn(*args: Any, **kwargs: Any) -> SimpleNamespace:
        if transcript_path is not None and transcript_lines is not None:
            transcript_path.write_text("\n".join(transcript_lines) + "\n", encoding="utf-8")
        for method, params in notifications:
            for handler in handlers.get(method, []):
                handler(method, params)
        return SimpleNamespace(id="turn-1")

    client = MagicMock()
    client.is_connected = True
    client.start = AsyncMock()
    client.stop = AsyncMock()
    client.add_notification_handler = MagicMock(side_effect=add_handler)
    client.remove_notification_handler = MagicMock()
    client.start_turn = AsyncMock(side_effect=start_turn)

    backend = CodexWebChatBackend(client=client)
    await backend.start()

    session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
    session._connected = True
    session._thread_id = "thread-1"
    if transcript_path is not None:
        transcript_path.write_text("", encoding="utf-8")
        session._transcript_path = str(transcript_path)
    else:
        session._get_transcript_offset = AsyncMock(return_value=0)
        session._get_transcript_assistant_text_since = AsyncMock(return_value=None)

    events = [event async for event in backend.send_message(session, "hello")]
    return events, session


class TestCodexBackend:
    @pytest.mark.asyncio
    async def test_attach_session_reuses_shared_client(self) -> None:
        client = MagicMock()
        client.is_connected = True
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.start_thread = AsyncMock(
            return_value=SimpleNamespace(id="thread-1", path="/tmp/codex.jsonl")
        )

        backend = CodexWebChatBackend(client=client)
        await backend.start()

        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session.project_path = "/tmp/project"
        await session.start(model="gpt-5.4")

        client.start_thread.assert_awaited_once_with(
            cwd="/tmp/project",
            model="gpt-5.4",
            approval_policy="on-request",
            sandbox=None,
        )
        assert session.sdk_session_id == "thread-1"
        assert session._thread_id == "thread-1"
        assert session._transcript_path == "/tmp/codex.jsonl"

    @pytest.mark.asyncio
    async def test_attach_session_passes_codex_sandbox_policy(self) -> None:
        client = MagicMock()
        client.is_connected = True
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.start_thread = AsyncMock(
            return_value=SimpleNamespace(id="thread-1", path="/tmp/codex.jsonl")
        )

        backend = CodexWebChatBackend(
            client=client,
            sandbox_config=SandboxConfig(enabled=True, mode="restrictive"),
        )
        await backend.start()

        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session.project_path = "/tmp/project"
        await session.start(model="gpt-5.4")

        client.start_thread.assert_awaited_once_with(
            cwd="/tmp/project",
            model="gpt-5.4",
            approval_policy="on-request",
            sandbox="read-only",
        )
        assert client.start_thread.await_count == 1
        assert client.start_thread.await_args is not None

    @pytest.mark.asyncio
    async def test_managed_session_delegates_send_message(self) -> None:
        backend = MagicMock()
        backend.attach_session = AsyncMock()
        backend.send_message = MagicMock(
            return_value=_async_stream(
                TextChunk(content="codex ok"),
                DoneEvent(tool_calls_count=0, sdk_session_id="thread-1"),
            )
        )

        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session._connected = True
        session.sdk_session_id = "thread-1"

        events = [event async for event in session.send_message("hello")]

        assert session.message_index == 1
        assert [e.content for e in events if isinstance(e, TextChunk)] == ["codex ok"]
        assert isinstance(events[-1], DoneEvent)

    @pytest.mark.asyncio
    async def test_send_message_passes_reasoning_effort_as_effort(self) -> None:
        handlers: dict[str, list[Any]] = {}

        def add_handler(method: str, handler: Any) -> None:
            handlers.setdefault(method, []).append(handler)

        async def start_turn(*args: Any, **kwargs: Any) -> SimpleNamespace:
            for handler in handlers.get("turn/completed", []):
                handler(
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    },
                )
            return SimpleNamespace(id="turn-1")

        client = MagicMock()
        client.is_connected = True
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.add_notification_handler = MagicMock(side_effect=add_handler)
        client.remove_notification_handler = MagicMock()
        client.start_turn = AsyncMock(side_effect=start_turn)

        backend = CodexWebChatBackend(client=client)
        await backend.start()

        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session._connected = True
        session._thread_id = "thread-1"
        session._model = "gpt-5.4"
        session._context_window_overrides = {"gpt-5.4": 200_000}
        session.reasoning_effort = "xhigh"
        session._get_transcript_offset = AsyncMock(return_value=0)
        session._get_transcript_assistant_text_since = AsyncMock(return_value=None)

        events = [event async for event in backend.send_message(session, "hello")]

        client.start_turn.assert_awaited_once_with(
            "thread-1",
            "hello",
            context_prefix=None,
            model="gpt-5.4",
            effort="xhigh",
        )
        assert isinstance(events[-1], DoneEvent)
        assert events[-1].context_window == 200_000

    @pytest.mark.asyncio
    async def test_send_message_emits_tool_call_event_for_started_item(self) -> None:
        events, _session = await _collect_codex_backend_events(
            [
                (
                    "item/started",
                    {
                        "threadId": "thread-1",
                        "item": {
                            "id": "item-mcp-1",
                            "type": "mcpToolCall",
                            "mcpToolCall": {
                                "server": "gobby-tasks",
                                "tool": "list_tasks",
                                "arguments": '{"status":"open"}',
                            },
                        },
                    },
                ),
                (
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    },
                ),
            ]
        )

        tool_calls = [event for event in events if isinstance(event, ToolCallEvent)]

        assert len(tool_calls) == 1
        assert tool_calls[0].tool_call_id == "item-mcp-1"
        assert tool_calls[0].tool_name == "mcp__gobby-tasks__list_tasks"
        assert tool_calls[0].server_name == "gobby-tasks"
        assert tool_calls[0].arguments == {"status": "open"}
        assert isinstance(events[-1], DoneEvent)
        assert events[-1].tool_calls_count == 1

    @pytest.mark.asyncio
    async def test_send_message_emits_tool_result_event_for_completed_item(self) -> None:
        events, _session = await _collect_codex_backend_events(
            [
                (
                    "item/started",
                    {
                        "threadId": "thread-1",
                        "item": {
                            "id": "item-mcp-1",
                            "type": "mcpToolCall",
                            "mcpToolCall": {
                                "server": "gobby-tasks",
                                "tool": "get_task",
                                "arguments": '{"task_id":"#42"}',
                            },
                        },
                    },
                ),
                (
                    "item/completed",
                    {
                        "threadId": "thread-1",
                        "item": {
                            "id": "item-mcp-1",
                            "type": "mcpToolCall",
                            "mcpToolCall": {
                                "server": "gobby-tasks",
                                "tool": "get_task",
                                "arguments": '{"task_id":"#42"}',
                            },
                            "result": {"id": "#42", "title": "Fix chat"},
                        },
                    },
                ),
                (
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    },
                ),
            ]
        )

        tool_results = [event for event in events if isinstance(event, ToolResultEvent)]

        assert len(tool_results) == 1
        assert tool_results[0].tool_call_id == "item-mcp-1"
        assert tool_results[0].success is True
        assert tool_results[0].result == {"id": "#42", "title": "Fix chat"}
        assert isinstance(events[-1], DoneEvent)
        assert events[-1].tool_calls_count == 1

    @pytest.mark.asyncio
    async def test_send_message_emits_fallback_tool_call_for_completed_item(self) -> None:
        events, _session = await _collect_codex_backend_events(
            [
                (
                    "item/completed",
                    {
                        "threadId": "thread-1",
                        "item": {
                            "id": "item-mcp-1",
                            "type": "mcpToolCall",
                            "mcpToolCall": {
                                "server": "gobby-tasks",
                                "tool": "close_task",
                                "arguments": '{"task_id":"#42"}',
                            },
                            "result": {"success": True},
                        },
                    },
                ),
                (
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    },
                ),
            ]
        )

        assert [type(event) for event in events] == [ToolCallEvent, ToolResultEvent, DoneEvent]
        assert events[0].tool_call_id == "item-mcp-1"
        assert events[1].tool_call_id == "item-mcp-1"
        assert events[1].result == {"success": True}
        assert events[2].tool_calls_count == 1

    @pytest.mark.asyncio
    async def test_send_message_normalizes_camel_and_snake_case_usage(self) -> None:
        events, _session = await _collect_codex_backend_events(
            [
                (
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {
                            "inputTokens": 5,
                            "output_tokens": 7,
                            "cacheReadInputTokens": 11,
                            "cache_creation_input_tokens": 13,
                        },
                    },
                ),
            ]
        )

        done = events[-1]

        assert isinstance(done, DoneEvent)
        assert done.input_tokens == 5
        assert done.output_tokens == 7
        assert done.cache_read_input_tokens == 11
        assert done.cache_creation_input_tokens == 13
        assert done.total_input_tokens == 29

    @pytest.mark.asyncio
    async def test_send_message_emits_tool_events_from_codex_response_items(self) -> None:
        events, _session = await _collect_codex_backend_events(
            [
                (
                    "response_item",
                    {
                        "type": "function_call",
                        "name": "call_tool",
                        "arguments": json.dumps(
                            {
                                "server_name": "gobby-tasks",
                                "tool_name": "get_task",
                                "arguments": {"task_id": "#42"},
                            }
                        ),
                        "call_id": "call-1",
                    },
                ),
                (
                    "response_item",
                    {
                        "type": "function_call_output",
                        "call_id": "call-1",
                        "output": json.dumps({"success": True}),
                    },
                ),
                (
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    },
                ),
            ]
        )

        tool_call = next(event for event in events if isinstance(event, ToolCallEvent))
        tool_result = next(event for event in events if isinstance(event, ToolResultEvent))

        assert tool_call.tool_call_id == "call-1"
        assert tool_call.tool_name == "call_tool"
        assert tool_call.server_name == "gobby-tasks"
        assert tool_call.arguments["tool_name"] == "get_task"
        assert tool_result.tool_call_id == "call-1"
        assert tool_result.result == {"success": True}
        assert events[-1].tool_calls_count == 1

    @pytest.mark.asyncio
    async def test_send_message_uses_token_count_event_for_done_usage(self) -> None:
        events, _session = await _collect_codex_backend_events(
            [
                (
                    "event_msg",
                    {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 26_435,
                                "cached_input_tokens": 25_984,
                                "output_tokens": 10,
                                "reasoning_output_tokens": 2,
                            },
                            "model_context_window": 258_400,
                        },
                    },
                ),
                (
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                ),
            ]
        )

        done = events[-1]

        assert isinstance(done, DoneEvent)
        assert done.input_tokens == 451
        assert done.cache_read_input_tokens == 25_984
        assert done.output_tokens == 12
        assert done.total_input_tokens == 26_435
        assert done.context_window == 258_400

    @pytest.mark.asyncio
    async def test_send_message_recovers_tool_and_usage_from_codex_transcript(
        self,
        tmp_path: Path,
    ) -> None:
        transcript_path = tmp_path / "codex.jsonl"
        transcript_lines = [
            json.dumps(
                {
                    "timestamp": "2026-05-13T18:26:09.726Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "call_tool",
                        "arguments": json.dumps(
                            {
                                "server_name": "gobby-tasks",
                                "tool_name": "get_task",
                                "arguments": {"task_id": "#14579"},
                            }
                        ),
                        "call_id": "call-1",
                    },
                }
            ),
            json.dumps(
                {
                    "timestamp": "2026-05-13T18:26:10.077Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "mcp_tool_call_end",
                        "call_id": "call-1",
                        "invocation": {
                            "server": "gobby",
                            "tool": "call_tool",
                            "arguments": {
                                "server_name": "gobby-tasks",
                                "tool_name": "get_task",
                                "arguments": {"task_id": "#14579"},
                            },
                        },
                        "result": {"Ok": {"structuredContent": {"success": True}}},
                    },
                }
            ),
            json.dumps(
                {
                    "timestamp": "2026-05-13T18:26:13.962Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 128_050,
                                "cached_input_tokens": 108_416,
                                "output_tokens": 214,
                                "reasoning_output_tokens": 82,
                            },
                            "model_context_window": 258_400,
                        },
                    },
                }
            ),
        ]

        events, _session = await _collect_codex_backend_events(
            [
                (
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                ),
            ],
            transcript_path=transcript_path,
            transcript_lines=transcript_lines,
        )

        tool_calls = [event for event in events if isinstance(event, ToolCallEvent)]
        tool_results = [event for event in events if isinstance(event, ToolResultEvent)]
        done = events[-1]

        assert len(tool_calls) == 1
        assert tool_calls[0].tool_call_id == "call-1"
        assert tool_calls[0].tool_name == "call_tool"
        assert len(tool_results) == 1
        assert tool_results[0].tool_call_id == "call-1"
        assert isinstance(done, DoneEvent)
        assert done.tool_calls_count == 1
        assert done.input_tokens == 19_634
        assert done.cache_read_input_tokens == 108_416
        assert done.output_tokens == 296
        assert done.total_input_tokens == 128_050
        assert done.context_window == 258_400

    @pytest.mark.asyncio
    async def test_interrupt_uses_thread_and_turn_identity(self) -> None:
        client = MagicMock()
        client.is_connected = True
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.interrupt_turn = AsyncMock()

        backend = CodexWebChatBackend(client=client)
        await backend.start()

        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session._thread_id = "thread-1"
        session._turn_id = "turn-9"

        await backend.interrupt(session)

        client.interrupt_turn.assert_awaited_once_with("thread-1", "turn-9")
        assert client.interrupt_turn.await_count == 1
        assert client.interrupt_turn.await_args is not None
        assert session._turn_id is None

    @pytest.mark.asyncio
    async def test_transcript_retry_uses_configured_timing(self, tmp_path: Path) -> None:
        transcript = tmp_path / "codex.jsonl"
        transcript.write_text("ignored\n", encoding="utf-8")

        session = CodexManagedChatSession(
            conversation_id="conv-codex",
            _backend=MagicMock(),
            _thread_id="thread-1",
            _transcript_path=str(transcript),
            _transcript_retry_attempts=2,
            _transcript_retry_delay_seconds=0.25,
        )

        recovered = ParsedMessage(
            index=0,
            role="assistant",
            content="Recovered from transcript",
            content_type="text",
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=datetime.now(UTC),
            raw_json={},
        )
        parsed_batches = [
            [],
            [recovered],
        ]

        sleep = AsyncMock()
        with (
            patch(
                "gobby.servers.websocket.chat.backends.codex.CodexTranscriptParser.parse_lines",
                side_effect=parsed_batches,
            ),
            patch("gobby.servers.websocket.chat.backends.codex.asyncio.sleep", sleep),
        ):
            assistant_text = await session._get_transcript_assistant_text_since(0)

        assert assistant_text == "Recovered from transcript"
        sleep.assert_awaited_once_with(0.25)
        assert sleep.await_count == 1
        assert sleep.await_args is not None

    @pytest.mark.asyncio
    async def test_handle_approval_request_accepts_decision_dict(self) -> None:
        backend = CodexWebChatBackend(client=MagicMock())
        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session.project_path = "/tmp/project"
        session.chat_mode = "accept_edits"
        session._thread_id = "thread-1"
        session._wait_for_tool_approval = AsyncMock(return_value={"decision": "accept"})
        backend._sessions_by_thread["thread-1"] = session

        with (
            patch.object(
                backend,
                "_translate_approval_request",
                return_value=("Write", {"file_path": "notes.md"}),
            ),
            patch(
                "gobby.servers.websocket.chat.backends.codex.find_out_of_repo_write_path",
                return_value=None,
            ),
            patch(
                "gobby.servers.websocket.chat.backends.codex.is_tool_auto_allowed",
                return_value=False,
            ),
        ):
            result = await backend.handle_approval_request("tools/call", {"threadId": "thread-1"})

        assert result == backend._accept_response("tools/call")
        assert session._wait_for_tool_approval.await_count == 1

    @pytest.mark.asyncio
    async def test_handle_approval_request_respects_managed_pre_tool_block(self) -> None:
        backend = CodexWebChatBackend(client=MagicMock())
        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session.project_path = "/tmp/project"
        session.chat_mode = "accept_edits"
        session._thread_id = "thread-1"
        session._on_pre_tool = AsyncMock(
            return_value={"decision": "block", "context": TASK_TRANSITIONS_SKILL_DIRECTIVE}
        )
        backend._sessions_by_thread["thread-1"] = session

        with (
            patch.object(
                backend,
                "_translate_approval_request",
                return_value=(
                    "mcp__gobby__call_tool",
                    {"server_name": "gobby-tasks", "tool_name": "close_task"},
                ),
            ),
            patch(
                "gobby.servers.websocket.chat.backends.codex.find_out_of_repo_write_path",
                return_value=None,
            ),
        ):
            result = await backend.handle_approval_request(
                "mcpServer/elicitation/request",
                {"threadId": "thread-1"},
            )

        assert result == backend._decline_response("mcpServer/elicitation/request")
        session._on_pre_tool.assert_awaited_once_with(
            {
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {"server_name": "gobby-tasks", "tool_name": "close_task"},
            }
        )
        assert session._consume_deferred_context() == TASK_TRANSITIONS_SKILL_DIRECTIVE

    @pytest.mark.asyncio
    async def test_handle_approval_request_allows_gcode_in_plan_mode(self) -> None:
        backend = CodexWebChatBackend(client=MagicMock())
        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session.project_path = "/tmp/project"
        session.chat_mode = "plan"
        session._thread_id = "thread-1"
        backend._sessions_by_thread["thread-1"] = session

        result = await backend.handle_approval_request(
            "item/commandExecution/requestApproval",
            {"threadId": "thread-1", "parsedCmd": 'gcode search "ChatSession"'},
        )

        assert result == backend._accept_response("item/commandExecution/requestApproval")

    @pytest.mark.asyncio
    async def test_handle_approval_request_blocks_gcode_redirection_in_plan_mode(self) -> None:
        backend = CodexWebChatBackend(client=MagicMock())
        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session.project_path = "/tmp/project"
        session.chat_mode = "plan"
        session._thread_id = "thread-1"
        backend._sessions_by_thread["thread-1"] = session

        result = await backend.handle_approval_request(
            "item/commandExecution/requestApproval",
            {"threadId": "thread-1", "parsedCmd": 'gcode search "ChatSession" > notes.txt'},
        )

        assert result == backend._decline_response("item/commandExecution/requestApproval")

    @pytest.mark.asyncio
    async def test_send_message_replays_deferred_context_prefix(self) -> None:
        backend = MagicMock()
        backend.attach_session = AsyncMock()
        backend.send_message = MagicMock(
            return_value=_async_stream(DoneEvent(tool_calls_count=0, sdk_session_id="thread-1"))
        )

        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session._connected = True
        session._thread_id = "thread-1"
        session._deferred_contexts.append(CODE_INDEX_SKILL_DIRECTIVE)

        events = [event async for event in session.send_message("hello")]

        assert isinstance(events[-1], DoneEvent)
        assert backend.send_message.call_args.kwargs["context_prefix"] is not None
        assert CODE_INDEX_SKILL_DIRECTIVE in backend.send_message.call_args.kwargs["context_prefix"]

    @pytest.mark.asyncio
    async def test_send_message_applies_post_tool_lifecycle_for_completed_items(self) -> None:
        handlers: dict[str, list[Any]] = {}

        def add_handler(method: str, handler: Any) -> None:
            handlers.setdefault(method, []).append(handler)

        async def start_turn(*args: Any, **kwargs: Any) -> SimpleNamespace:
            for handler in handlers.get("item/completed", []):
                handler(
                    "item/completed",
                    {
                        "threadId": "thread-1",
                        "item": {
                            "type": "mcpToolCall",
                            "server": "gobby-tasks",
                            "tool": "close_task",
                            "arguments": {"task_id": "#42"},
                            "result": {"success": True},
                        },
                    },
                )
            for handler in handlers.get("turn/completed", []):
                handler(
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    },
                )
            return SimpleNamespace(id="turn-1")

        client = MagicMock()
        client.is_connected = True
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.add_notification_handler = MagicMock(side_effect=add_handler)
        client.remove_notification_handler = MagicMock()
        client.start_turn = AsyncMock(side_effect=start_turn)

        backend = CodexWebChatBackend(client=client)
        await backend.start()

        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session._connected = True
        session._thread_id = "thread-1"
        session._on_post_tool = AsyncMock(
            return_value={"context": TASK_TRANSITIONS_SKILL_DIRECTIVE}
        )
        session._get_transcript_offset = AsyncMock(return_value=0)
        session._get_transcript_assistant_text_since = AsyncMock(return_value=None)

        events = [event async for event in backend.send_message(session, "hello")]

        assert isinstance(events[-1], DoneEvent)
        session._on_post_tool.assert_awaited_once_with(
            {
                "tool_name": "mcp__gobby-tasks__close_task",
                "tool_input": {"task_id": "#42"},
                "tool_response": {"success": True},
                "mcp_server": "gobby-tasks",
                "mcp_tool": "close_task",
            }
        )
        assert session._consume_deferred_context() == TASK_TRANSITIONS_SKILL_DIRECTIVE

    @pytest.mark.asyncio
    async def test_send_message_normalizes_realistic_completed_mcp_items(self) -> None:
        handlers: dict[str, list[Any]] = {}

        def add_handler(method: str, handler: Any) -> None:
            handlers.setdefault(method, []).append(handler)

        async def start_turn(*args: Any, **kwargs: Any) -> SimpleNamespace:
            completed_events = [
                {
                    "threadId": "thread-other",
                    "item": {
                        "id": "item-other",
                        "type": "mcpToolCall",
                        "status": "completed",
                        "mcpToolCall": {
                            "server": "gobby-tasks",
                            "tool": "close_task",
                            "arguments": '{"task_id":"#7"}',
                        },
                        "result": {"success": True},
                    },
                },
                {
                    "threadId": "thread-1",
                    "item": {
                        "id": "item-compact-1",
                        "type": ">>>contextCompaction<<<",
                        "status": "completed",
                    },
                },
                {
                    "threadId": "thread-1",
                    "item": {
                        "id": "item-msg-1",
                        "type": "assistantMessage",
                        "status": "completed",
                        "assistantMessage": {"content": [{"type": "output_text", "text": "hello"}]},
                    },
                },
                {
                    "threadId": "thread-1",
                    "item": {
                        "id": "item-mcp-1",
                        "type": "mcpToolCall",
                        "status": "completed",
                        "mcpToolCall": {
                            "server": "gobby-tasks",
                            "tool": "close_task",
                            "arguments": '{"task_id":"#42","changes_summary":"done"}',
                        },
                        "result": {"success": True, "task_id": "#42"},
                    },
                },
            ]
            for params in completed_events:
                for handler in handlers.get("item/completed", []):
                    handler("item/completed", params)
            for handler in handlers.get("turn/completed", []):
                handler(
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    },
                )
            return SimpleNamespace(id="turn-1")

        client = MagicMock()
        client.is_connected = True
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.add_notification_handler = MagicMock(side_effect=add_handler)
        client.remove_notification_handler = MagicMock()
        client.start_turn = AsyncMock(side_effect=start_turn)

        backend = CodexWebChatBackend(client=client)
        await backend.start()

        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session._connected = True
        session._thread_id = "thread-1"
        session._on_post_tool = AsyncMock()
        session._get_transcript_offset = AsyncMock(return_value=0)
        session._get_transcript_assistant_text_since = AsyncMock(return_value=None)

        events = [event async for event in backend.send_message(session, "close the task")]

        assert isinstance(events[-1], DoneEvent)
        session._on_post_tool.assert_awaited_once_with(
            {
                "tool_name": "mcp__gobby-tasks__close_task",
                "tool_input": {"task_id": "#42", "changes_summary": "done"},
                "tool_response": {"success": True, "task_id": "#42"},
                "mcp_server": "gobby-tasks",
                "mcp_tool": "close_task",
            }
        )
        assert session._on_post_tool.await_count == 1
        assert session._on_post_tool.await_args is not None

    def test_translate_approval_request_parses_json_string_arguments_for_mcp_tool_call(
        self,
    ) -> None:
        backend = CodexWebChatBackend(client=MagicMock())

        tool_name, input_data = backend._translate_approval_request(
            "item/mcpToolCall/requestApproval",
            {
                "threadId": "thread-1",
                "itemId": "item-mcp-1",
                "serverName": "gobby-tasks",
                "name": "close_task",
                "arguments": '{"task_id":"#42","changes_summary":"done"}',
            },
        )

        assert tool_name == "mcp__gobby__call_tool"
        assert input_data == {
            "task_id": "#42",
            "changes_summary": "done",
            "server_name": "gobby-tasks",
            "tool_name": "close_task",
        }

    @pytest.mark.asyncio
    async def test_send_message_dispatches_pre_tool_once_per_item_and_resets_each_turn(
        self,
    ) -> None:
        handlers: dict[str, list[Any]] = {}
        turn_count = 0

        def add_handler(method: str, handler: Any) -> None:
            handlers.setdefault(method, []).append(handler)

        async def start_turn(*args: Any, **kwargs: Any) -> SimpleNamespace:
            nonlocal turn_count
            turn_count += 1
            turn_id = f"turn-{turn_count}"
            for handler in handlers.get("turn/started", []):
                handler("turn/started", {"threadId": "thread-1", "turn": {"id": turn_id}})
            for handler in handlers.get("item/started", []):
                handler(
                    "item/started",
                    {
                        "threadId": "thread-1",
                        "turnId": turn_id,
                        "itemId": "item-mcp-1",
                        "item": {
                            "id": "item-mcp-1",
                            "type": "mcpToolCall",
                            "mcpToolCall": {
                                "server": "gobby-tasks",
                                "tool": "close_task",
                                "arguments": '{"task_id":"#42"}',
                            },
                        },
                    },
                )
                handler(
                    "item/started",
                    {
                        "threadId": "thread-1",
                        "turnId": turn_id,
                        "itemId": "item-mcp-1",
                        "item": {
                            "id": "item-mcp-1",
                            "type": "mcpToolCall",
                            "mcpToolCall": {
                                "server": "gobby-tasks",
                                "tool": "close_task",
                                "arguments": '{"task_id":"#42"}',
                            },
                        },
                    },
                )
            for handler in handlers.get("turn/completed", []):
                handler(
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": turn_id},
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    },
                )
            return SimpleNamespace(id=turn_id)

        client = MagicMock()
        client.is_connected = True
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.add_notification_handler = MagicMock(side_effect=add_handler)
        client.remove_notification_handler = MagicMock()
        client.start_turn = AsyncMock(side_effect=start_turn)

        backend = CodexWebChatBackend(client=client)
        await backend.start()

        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session._connected = True
        session._thread_id = "thread-1"
        session.chat_mode = "bypass"
        session._on_pre_tool = AsyncMock()
        session._get_transcript_offset = AsyncMock(return_value=0)
        session._get_transcript_assistant_text_since = AsyncMock(return_value=None)
        backend._sessions_by_thread["thread-1"] = session

        [event async for event in backend.send_message(session, "first turn")]
        with patch(
            "gobby.servers.websocket.chat.backends.codex.find_out_of_repo_write_path",
            return_value=None,
        ):
            approval_result = await backend.handle_approval_request(
                "item/mcpToolCall/requestApproval",
                {
                    "threadId": "thread-1",
                    "itemId": "item-mcp-1",
                    "serverName": "gobby-tasks",
                    "name": "close_task",
                    "arguments": '{"task_id":"#42"}',
                },
            )
        [event async for event in backend.send_message(session, "second turn")]

        assert approval_result == backend._accept_response("item/mcpToolCall/requestApproval")
        assert session._on_pre_tool.await_args_list == [
            call(
                {
                    "tool_name": "mcp__gobby-tasks__close_task",
                    "tool_input": {"task_id": "#42"},
                }
            ),
            call(
                {
                    "tool_name": "mcp__gobby-tasks__close_task",
                    "tool_input": {"task_id": "#42"},
                }
            ),
        ]
