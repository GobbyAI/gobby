"""Tests for ChatSession-backed communications delivery."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.communications.chat_backend import ChatSessionCommsBackend
from gobby.communications.chat_transport import CommunicationsChatStreamTransport
from gobby.communications.models import ChannelConfig, CommsAttachment, CommsMessage
from gobby.communications.responder import ResponderContext
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.voice.tts import TTSProvider


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _FakeManager:
    def __init__(
        self,
        *,
        supports_edit: bool,
        supports_typing: bool = False,
        attachment_status: str = "sent",
    ) -> None:
        self.supports_edit = supports_edit
        self.typing_supported = supports_typing
        self.attachment_status = attachment_status
        self.sent: list[tuple[str, str, str | None, dict[str, object] | None]] = []
        self.edited: list[tuple[str, str, str, str]] = []
        self.typing: list[tuple[str, str]] = []
        self.attachments: list[dict[str, object]] = []
        self.stored_messages: list[CommsMessage] = []
        self.attachment_manager = _FakeAttachmentManager()

    def supports_message_edit(self, channel_name: str) -> bool:
        assert channel_name == "telegram"
        return self.supports_edit

    def supports_typing(self, channel_name: str) -> bool:
        assert channel_name == "telegram"
        return self.typing_supported

    async def send_typing(self, channel_name: str, conversation_id: str) -> None:
        self.typing.append((channel_name, conversation_id))

    async def send_message(
        self,
        channel_name: str,
        content: str,
        session_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> CommsMessage:
        self.sent.append((channel_name, content, session_id, metadata))
        return CommsMessage(
            id=f"out-{len(self.sent)}",
            channel_id="channel-1",
            direction="outbound",
            content=content,
            created_at=datetime.now(UTC),
            platform_message_id=f"platform-{len(self.sent)}",
            session_id=session_id,
            metadata_json=metadata or {},
        )

    async def edit_message(
        self,
        channel_name: str,
        platform_message_id: str,
        content: str,
        conversation_id: str,
    ) -> None:
        self.edited.append((channel_name, platform_message_id, content, conversation_id))

    async def send_attachment(
        self,
        channel_name: str,
        file_path: Path,
        filename: str | None = None,
        content_type: str = "application/octet-stream",
        content: str = "",
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[CommsMessage, CommsAttachment]:
        self.attachments.append(
            {
                "channel_name": channel_name,
                "file_path": file_path,
                "filename": filename,
                "content_type": content_type,
                "content": content,
                "session_id": session_id,
                "metadata": metadata,
            }
        )
        message = CommsMessage(
            id=f"attachment-message-{len(self.attachments)}",
            channel_id="channel-1",
            direction="outbound",
            content=content,
            content_type="attachment",
            status=self.attachment_status,
            error="voice delivery failed" if self.attachment_status != "sent" else None,
            created_at=datetime.now(UTC),
        )
        attachment = CommsAttachment(
            id=f"attachment-{len(self.attachments)}",
            message_id=message.id,
            filename=filename or file_path.name,
            content_type=content_type,
            size_bytes=4,
            local_path=str(file_path),
            created_at=datetime.now(UTC),
        )
        return message, attachment

    def list_messages(
        self,
        *,
        channel_id: str | None = None,
        session_id: str | None = None,
        direction: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CommsMessage]:
        assert channel_id == "channel-1"
        assert session_id == "session-1"
        assert direction == "inbound"
        return self.stored_messages[offset : offset + limit]


class _FakeAttachmentManager:
    def __init__(self) -> None:
        self.stored: list[tuple[bytes, str]] = []

    async def store(self, content: bytes, filename: str) -> Path:
        self.stored.append((content, filename))
        return Path("/virtual") / filename


def _context(
    *,
    content: str = "hello",
    session_id: str = "session-1",
    responder_config: dict[str, Any] | None = None,
) -> ResponderContext:
    channel = ChannelConfig(
        id="channel-1",
        channel_type="telegram",
        name="telegram",
        enabled=True,
        config_json={"responder": {"enabled": True}},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    message = CommsMessage(
        id=f"in-{content}",
        channel_id=channel.id,
        direction="inbound",
        content=content,
        created_at=datetime.now(UTC),
        session_id=session_id,
        metadata_json={"platform_channel_id": "chat-42"},
    )
    return ResponderContext(
        channel=channel,
        message=message,
        conversation_id="chat-42",
        sender_id="user-7",
        is_group=False,
        responder_config=(
            responder_config
            if responder_config is not None
            else {"provider": "codex", "model": "gpt-5.6"}
        ),
    )


@pytest.mark.asyncio
async def test_transport_collects_stream_and_sends_one_final_fallback() -> None:
    manager = _FakeManager(supports_edit=False)
    transport = CommunicationsChatStreamTransport(manager, _context())

    assert await transport.safe_send({"type": "chat_stream", "content": "Hello", "done": False})
    assert await transport.safe_send({"type": "chat_stream", "content": " world", "done": False})
    assert manager.sent == []

    assert await transport.safe_send({"type": "chat_stream", "content": "", "done": True})

    assert transport.text == "Hello world"
    assert manager.sent == [
        (
            "telegram",
            "Hello world",
            "session-1",
            {"platform_destination": "chat-42"},
        )
    ]
    assert manager.edited == []


@pytest.mark.asyncio
async def test_transport_throttles_edits_and_flushes_latest_text() -> None:
    manager = _FakeManager(supports_edit=True)
    clock = _Clock()
    transport = CommunicationsChatStreamTransport(
        manager,
        _context(),
        edit_interval=1.5,
        clock=clock,
    )

    await transport.safe_send({"type": "chat_stream", "content": "A", "done": False})
    assert [sent[1] for sent in manager.sent] == ["Thinking…"]

    clock.now = 0.5
    await transport.safe_send({"type": "chat_stream", "content": "B", "done": False})
    assert manager.edited == []

    clock.now = 1.5
    await transport.safe_send({"type": "chat_stream", "content": "C", "done": False})
    assert manager.edited == [("telegram", "platform-1", "ABC", "chat-42")]

    clock.now = 1.6
    await transport.safe_send({"type": "chat_stream", "content": "D", "done": False})
    assert len(manager.edited) == 1

    await transport.safe_send({"type": "chat_stream", "content": "", "done": True})

    assert transport.text == "ABCD"
    assert manager.edited[-1] == ("telegram", "platform-1", "ABCD", "chat-42")


@pytest.mark.asyncio
async def test_transport_replaces_placeholder_with_short_final_text() -> None:
    manager = _FakeManager(supports_edit=True)
    transport = CommunicationsChatStreamTransport(manager, _context())

    await transport.safe_send({"type": "chat_stream", "content": "OK", "done": False})
    await transport.safe_send({"type": "chat_stream", "content": "", "done": True})

    assert [sent[1] for sent in manager.sent] == ["Thinking…"]
    assert manager.edited == [("telegram", "platform-1", "OK", "chat-42")]


class _FakeChatHost:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.configurations: list[dict[str, str]] = []
        self.sessions: dict[str, object] = {}

    async def configure_chat_session(
        self,
        conversation_id: str,
        *,
        chat_mode: str,
        agent_name: str,
        project_id: str,
    ) -> None:
        self.configurations.append(
            {
                "conversation_id": conversation_id,
                "chat_mode": chat_mode,
                "agent_name": agent_name,
                "project_id": project_id,
            }
        )

    async def _run_chat_turn(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        conversation_id = str(kwargs["conversation_id"])
        self.sessions.setdefault(conversation_id, object())
        transport = kwargs["transport"]
        await transport.safe_send(
            {
                "type": "chat_stream",
                "content": f"reply:{kwargs['content']}",
                "done": False,
            }
        )
        await transport.safe_send({"type": "chat_stream", "content": "", "done": True})

    async def reset_chat_session(self, conversation_id: str) -> bool:
        return self.sessions.pop(conversation_id, None) is not None

    def resolve_chat_binding(
        self,
        conversation_id: str,
        *,
        provider: str | None,
        model: str | None,
    ) -> tuple[str, str | None]:
        assert conversation_id == "session-1"
        return provider or "codex", model or "gpt-5.6-sol"


class _IncompleteChatHost(_FakeChatHost):
    async def _run_chat_turn(self, **kwargs: Any) -> None:
        transport = kwargs["transport"]
        await transport.safe_send(
            {"type": "chat_stream", "content": "partial reply", "done": False}
        )


@pytest.mark.asyncio
async def test_backend_reuses_comms_session_key_and_propagates_provider_model() -> None:
    manager = _FakeManager(supports_edit=False)
    host = _FakeChatHost()
    backend = ChatSessionCommsBackend(host, manager)

    first_context = _context(content="first")
    second_context = _context(content="second")
    await backend.run_turn(first_context)
    first_session = host.sessions["session-1"]
    await backend.run_turn(second_context)

    assert host.sessions["session-1"] is first_session
    assert [call["conversation_id"] for call in host.calls] == ["session-1", "session-1"]
    assert [call["provider"] for call in host.calls] == ["codex", "codex"]
    assert [call["model"] for call in host.calls] == ["gpt-5.6", "gpt-5.6"]
    assert [call["project_id"] for call in host.calls] == [
        PERSONAL_PROJECT_ID,
        PERSONAL_PROJECT_ID,
    ]
    assert host.configurations == [
        {
            "conversation_id": "session-1",
            "chat_mode": "normal",
            "agent_name": "comms-agent",
            "project_id": PERSONAL_PROJECT_ID,
        },
        {
            "conversation_id": "session-1",
            "chat_mode": "normal",
            "agent_name": "comms-agent",
            "project_id": PERSONAL_PROJECT_ID,
        },
    ]
    assert [sent[1] for sent in manager.sent] == ["reply:first", "reply:second"]


@pytest.mark.asyncio
async def test_backend_honors_explicit_surface_context() -> None:
    manager = _FakeManager(supports_edit=False)
    host = _FakeChatHost()
    backend = ChatSessionCommsBackend(host, manager)

    await backend.run_turn(
        _context(
            responder_config={
                "agent": "researcher",
                "chat_mode": "plan",
                "project_id": "project-1",
            }
        )
    )

    assert host.configurations == [
        {
            "conversation_id": "session-1",
            "chat_mode": "plan",
            "agent_name": "researcher",
            "project_id": "project-1",
        }
    ]
    assert host.calls[0]["project_id"] == "project-1"


@pytest.mark.asyncio
async def test_backend_flushes_fallback_when_stream_ends_without_done_event() -> None:
    manager = _FakeManager(supports_edit=False)
    backend = ChatSessionCommsBackend(_IncompleteChatHost(), manager)

    await backend.run_turn(_context())

    assert [sent[1] for sent in manager.sent] == ["partial reply"]


@pytest.mark.asyncio
async def test_backend_publishes_typing_indicator_for_supported_channel() -> None:
    manager = _FakeManager(supports_edit=False, supports_typing=True)
    backend = ChatSessionCommsBackend(_FakeChatHost(), manager)

    await backend.run_turn(_context())

    assert manager.typing == [("telegram", "chat-42")]


@pytest.mark.asyncio
async def test_backend_status_reports_resolved_default_binding() -> None:
    manager = _FakeManager(supports_edit=False)
    backend = ChatSessionCommsBackend(_FakeChatHost(), manager)

    status = await backend.status(_context(responder_config={}))

    assert status == "Responder idle. Provider: codex. Model: gpt-5.6-sol."


@pytest.mark.asyncio
async def test_backend_status_reports_explicit_binding() -> None:
    manager = _FakeManager(supports_edit=False)
    backend = ChatSessionCommsBackend(_FakeChatHost(), manager)

    status = await backend.status(_context())

    assert status == "Responder idle. Provider: codex. Model: gpt-5.6."


@pytest.mark.asyncio
async def test_backend_sends_configured_telegram_tts_as_voice_note() -> None:
    manager = _FakeManager(supports_edit=True)
    host = _FakeChatHost()
    provider = object()
    synthesized: list[tuple[object, str]] = []

    async def synthesize(tts: TTSProvider, text: str) -> bytes:
        synthesized.append((tts, text))
        return b"OggSvoice"

    backend = ChatSessionCommsBackend(
        host,
        manager,
        tts_provider_getter=lambda: cast(TTSProvider, provider),
        voice_synthesizer=synthesize,
    )

    await backend.run_turn(_context(content="voice", responder_config={"tts_enabled": True}))

    assert manager.sent == []
    assert synthesized == [(provider, "reply:voice")]
    assert manager.attachment_manager.stored == [(b"OggSvoice", "reply.ogg")]
    assert manager.attachments == [
        {
            "channel_name": "telegram",
            "file_path": Path("/virtual/reply.ogg"),
            "filename": "reply.ogg",
            "content_type": "audio/ogg",
            "content": "reply:voice",
            "session_id": "session-1",
            "metadata": {
                "platform_destination": "chat-42",
                "voice_note": True,
            },
        }
    ]


@pytest.mark.asyncio
async def test_backend_falls_back_to_text_when_telegram_tts_fails() -> None:
    manager = _FakeManager(supports_edit=True)
    host = _FakeChatHost()

    async def fail_synthesis(tts: TTSProvider, text: str) -> bytes:
        raise RuntimeError("synthesis failed")

    backend = ChatSessionCommsBackend(
        host,
        manager,
        tts_provider_getter=lambda: cast(TTSProvider, object()),
        voice_synthesizer=fail_synthesis,
    )

    await backend.run_turn(_context(content="fallback", responder_config={"tts_enabled": True}))

    assert [sent[1] for sent in manager.sent] == ["reply:fallback"]
    assert manager.attachments == []


@pytest.mark.asyncio
async def test_backend_falls_back_to_text_when_voice_note_delivery_fails() -> None:
    manager = _FakeManager(supports_edit=True, attachment_status="failed")
    host = _FakeChatHost()

    async def synthesize(tts: TTSProvider, text: str) -> bytes:
        return b"OggSvoice"

    backend = ChatSessionCommsBackend(
        host,
        manager,
        tts_provider_getter=lambda: cast(TTSProvider, object()),
        voice_synthesizer=synthesize,
    )

    await backend.run_turn(_context(content="delivery", responder_config={"tts_enabled": True}))

    assert [sent[1] for sent in manager.sent] == ["reply:delivery"]
    assert len(manager.attachments) == 1


@pytest.mark.asyncio
async def test_group_wake_includes_passive_messages_since_previous_wake() -> None:
    manager = _FakeManager(supports_edit=False)
    host = _FakeChatHost()
    backend = ChatSessionCommsBackend(host, manager)
    context = replace(_context(content="@gobby_bot what did I miss?"), is_group=True)
    context.message.metadata_json["passive_context"] = False
    context.message.metadata_json["conversation_type"] = "supergroup"
    previous_wake = CommsMessage(
        id="previous-wake",
        channel_id="channel-1",
        direction="inbound",
        content="@gobby_bot earlier question",
        session_id="session-1",
        metadata_json={"passive_context": False},
        created_at=datetime.now(UTC),
    )
    first_passive = CommsMessage(
        id="passive-1",
        channel_id="channel-1",
        direction="inbound",
        content="The deploy finished.",
        session_id="session-1",
        metadata_json={"passive_context": True, "external_username": "alice"},
        created_at=datetime.now(UTC),
    )
    second_passive = CommsMessage(
        id="passive-2",
        channel_id="channel-1",
        direction="inbound",
        content="Smoke tests are green.",
        session_id="session-1",
        metadata_json={"passive_context": True, "external_username": "bob"},
        created_at=datetime.now(UTC),
    )
    manager.stored_messages = [
        context.message,
        second_passive,
        first_passive,
        previous_wake,
    ]

    await backend.run_turn(context)

    assert host.calls[0]["content"] == (
        "Recent passive group context (oldest to newest):\n"
        "- alice: The deploy finished.\n"
        "- bob: Smoke tests are green.\n\n"
        "Current wake message:\n"
        "@gobby_bot what did I miss?"
    )
