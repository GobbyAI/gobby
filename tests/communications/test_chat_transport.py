"""Tests for ChatSession-backed communications delivery."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.communications.chat_backend import ChatSessionCommsBackend
from gobby.communications.chat_transport import CommunicationsChatStreamTransport
from gobby.communications.models import ChannelConfig, CommsMessage
from gobby.communications.responder import ResponderContext
from gobby.storage.projects import PERSONAL_PROJECT_ID


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _FakeManager:
    def __init__(self, *, supports_edit: bool, supports_typing: bool = False) -> None:
        self.supports_edit = supports_edit
        self.typing_supported = supports_typing
        self.sent: list[tuple[str, str, str | None, dict[str, object] | None]] = []
        self.edited: list[tuple[str, str, str, str]] = []
        self.typing: list[tuple[str, str]] = []

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
