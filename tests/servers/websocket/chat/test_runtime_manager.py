"""Tests for shared web-chat runtime manager and provider backends."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.adapters.gemini_acp_client import StreamEvent
from gobby.llm.claude_models import DoneEvent, TextChunk
from gobby.servers.chat_session import ChatSession
from gobby.servers.websocket.chat.provider_backends import (
    CodexManagedChatSession,
    CodexWebChatBackend,
    GeminiManagedChatSession,
    GeminiWebChatBackend,
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
        codex_session = manager.create_session(provider="codex", conversation_id="conv-3")

        assert isinstance(claude_session, ChatSession)
        assert isinstance(gemini_session, GeminiManagedChatSession)
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


class TestGeminiBackend:
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

        client.start_thread.assert_awaited_once_with(cwd="/tmp/project", model="gpt-5.4")
        assert session.sdk_session_id == "thread-1"
        assert session._thread_id == "thread-1"
        assert session._transcript_path == "/tmp/codex.jsonl"

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
