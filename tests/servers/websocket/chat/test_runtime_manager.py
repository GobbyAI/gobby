"""Tests for shared web-chat runtime manager and provider backends."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.adapters.gemini_acp_client import StreamEvent
from gobby.agents.sandbox import SandboxConfig
from gobby.config.app import DaemonConfig
from gobby.llm.claude_models import DoneEvent, TextChunk
from gobby.servers.chat_session import ChatSession
from gobby.servers.websocket.chat.provider_backends import (
    CodexManagedChatSession,
    CodexWebChatBackend,
    GeminiManagedChatSession,
    GeminiWebChatBackend,
    QwenManagedChatSession,
    QwenWebChatBackend,
)
from gobby.servers.websocket.chat.runtime_manager import WebChatRuntimeManager

pytestmark = pytest.mark.unit


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

    def test_manager_defaults_web_chat_sandbox_to_enabled(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None, daemon_config=DaemonConfig())

        assert manager.sandbox_config.enabled is True
        assert manager.sandbox_policy_hash


class TestGeminiBackend:
    def test_backend_does_not_build_full_process_sandboxed_acp_client(self) -> None:
        with patch("gobby.servers.websocket.chat.provider_backends.GeminiACPClient") as mock_client:
            GeminiWebChatBackend(sandbox_config=SandboxConfig(enabled=True, allow_network=False))

        kwargs = mock_client.call_args.kwargs
        assert kwargs["cli_name"] == "gemini"
        assert kwargs["display_name"] == "Gemini"
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
        session.sdk_session_id = "sess-1"

        events = [event async for event in session.send_message("hi")]

        assert [e.content for e in events if isinstance(e, TextChunk)] == ["Hello ", "Gemini"]
        assert isinstance(events[-1], DoneEvent)


class TestQwenBackend:
    def test_backend_does_not_build_full_process_sandboxed_acp_client(self) -> None:
        with patch("gobby.servers.websocket.chat.provider_backends.GeminiACPClient") as mock_client:
            QwenWebChatBackend(sandbox_config=SandboxConfig(enabled=True, allow_network=False))

        kwargs = mock_client.call_args.kwargs
        assert kwargs["cli_name"] == "qwen"
        assert kwargs["display_name"] == "Qwen"
        assert kwargs["prompt_timeout_env"] == "GOBBY_QWEN_ACP_PROMPT_TIMEOUT_SECONDS"
        assert "extra_args" not in kwargs
        assert "env_overrides" not in kwargs


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
            approval_policy="unlessTrusted",
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
            approval_policy="unlessTrusted",
            sandbox="read-only",
        )

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

        parsed_batches = [
            [],
            [SimpleNamespace(role="assistant", content="Recovered from transcript")],
        ]

        sleep = AsyncMock()
        with (
            patch(
                "gobby.servers.websocket.chat.provider_backends.CodexTranscriptParser.parse_lines",
                side_effect=parsed_batches,
            ),
            patch("gobby.servers.websocket.chat.provider_backends.asyncio.sleep", sleep),
        ):
            assistant_text = await session._get_transcript_assistant_text_since(0)

        assert assistant_text == "Recovered from transcript"
        sleep.assert_awaited_once_with(0.25)

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
                "gobby.servers.websocket.chat.provider_backends.find_out_of_repo_write_path",
                return_value=None,
            ),
            patch(
                "gobby.servers.websocket.chat.provider_backends.is_tool_auto_allowed",
                return_value=False,
            ),
        ):
            result = await backend.handle_approval_request("tools/call", {"threadId": "thread-1"})

        assert result == backend._accept_response("tools/call")
