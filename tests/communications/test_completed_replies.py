"""Regression coverage for completed responder delivery and session attribution."""

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.communications.chat_backend import ChatSessionCommsBackend
from gobby.communications.chat_transport import CommunicationsChatStreamTransport
from gobby.communications.identities import IdentityManager
from gobby.communications.models import ChannelConfig, CommsIdentity
from gobby.config.communications import CommunicationsConfig
from gobby.servers.websocket.chat._streaming import ChatStreamingMixin
from gobby.storage.communications import LocalCommunicationsStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.datetime import utc_now
from tests.communications.test_chat_transport import _context, _FakeChatHost, _FakeManager


@pytest.mark.asyncio
async def test_telegram_waits_for_complete_reply_and_delivers_whole_text_once() -> None:
    manager = _FakeManager(supports_edit=True)
    transport = CommunicationsChatStreamTransport(manager, _context())
    await transport.safe_send({"type": "chat_stream", "content": "Checking the route."})
    await transport.safe_send({"type": "tool_status", "status": "calling"})
    assert manager.sent == []

    answer = "\n\nThis is the completed answer. " + "Full reply. " * 800
    for chunk in (answer[:6], answer[6:80], answer[80:]):
        await transport.safe_send({"type": "chat_stream", "content": chunk})
        assert manager.sent == []
        assert manager.edited == []
    await transport.safe_send({"type": "chat_stream", "done": True})
    await transport.safe_send({"type": "chat_stream", "done": True})
    await transport.finalize()
    assert [message[1] for message in manager.sent] == [answer]
    assert manager.edited == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["eof", "interrupted", "error"])
async def test_unfinished_reply_is_never_delivered_as_completed(ending: str) -> None:
    manager = _FakeManager(supports_edit=False)
    transport = CommunicationsChatStreamTransport(manager, _context())
    await transport.safe_send({"type": "chat_stream", "content": "\n\nThis"})
    if ending == "interrupted":
        await transport.send_direct({"type": "chat_stream", "done": True, "interrupted": True})
    elif ending == "error":
        await transport.send_direct({"type": "chat_error", "error": "Provider unavailable."})
    await transport.finalize()
    assert [message[1] for message in manager.sent] == (
        ["Provider unavailable."] if ending == "error" else []
    )


@pytest.mark.asyncio
async def test_completed_reply_uses_authoritative_session_info() -> None:
    manager = _FakeManager(supports_edit=False)
    context = _context(session_id="expired-identity-session")
    transport = CommunicationsChatStreamTransport(manager, context)
    await transport.send_direct({"type": "session_info", "db_session_id": "live-web-chat"})
    await transport.safe_send({"type": "chat_stream", "content": "Whole answer", "done": True})
    assert [(message[1], message[2]) for message in manager.sent] == [
        ("Whole answer", "live-web-chat")
    ]


@pytest.mark.asyncio
async def test_cached_chat_turn_announces_effective_session_before_output() -> None:
    async def stream(_content: object) -> AsyncIterator[object]:
        if False:
            yield None

    host = ChatStreamingMixin()
    session = SimpleNamespace(db_session_id="live-web-chat", model=None, send_message=stream)
    host._chat_sessions = {"expired-routing-key": session}
    host._active_chat_tasks = {}
    transport = MagicMock()
    transport.base_msg.side_effect = dict
    transport.send_direct = AsyncMock()
    transport.safe_send = AsyncMock(return_value=True)
    await host._run_chat_turn(
        conversation_id="expired-routing-key", content="Hello", model=None, transport=transport
    )
    frames = [call.args[0] for call in transport.send_direct.await_args_list]
    assert [(frame["type"], frame.get("db_session_id")) for frame in frames] == [
        ("session_info", "live-web-chat")
    ]


@pytest.mark.parametrize("status", ["expired", "deleted"])
def test_expired_identity_recovers_a_live_sender_session(
    temp_db: HubDatabase, sample_project: dict[str, str], status: str
) -> None:
    sessions = SessionManager(temp_db)
    old = sessions.register("old-cli", None, "codex", sample_project["id"])
    sessions.update_status(old.id, status)
    store = LocalCommunicationsStore(temp_db)
    now = utc_now()
    channel = store.create_channel(
        ChannelConfig(
            id="",
            channel_type="telegram",
            name="test-recovery",
            enabled=True,
            config_json={},
            created_at=now,
            updated_at=now,
        )
    )
    identity = store.create_identity(
        CommsIdentity(
            id="",
            channel_id=channel.id,
            external_user_id="user-42",
            created_at=now,
            updated_at=now,
            session_id=old.id,
        )
    )
    manager = IdentityManager(store, sessions, CommunicationsConfig())
    resolved = manager.resolve_inbound_identity(
        channel.id, "user-42", project_id=sample_project["id"]
    )
    assert resolved.session_id != old.id
    assert resolved.session_id is not None
    current = sessions.get(resolved.session_id)
    assert current is not None and current.status == "active"
    saved = store.get_identity(identity.id)
    assert saved is not None and saved.session_id == current.id
    again = manager.resolve_inbound_identity(channel.id, "user-42", project_id=sample_project["id"])
    assert again.session_id == current.id


@pytest.mark.asyncio
async def test_incomplete_voice_reply_is_not_synthesized_or_sent() -> None:
    class IncompleteHost(_FakeChatHost):
        async def _run_chat_turn(self, **kwargs: object) -> None:
            transport = kwargs["transport"]
            assert isinstance(transport, CommunicationsChatStreamTransport)
            await transport.safe_send({"type": "chat_stream", "content": "partial"})

    manager = _FakeManager(supports_edit=False)
    synthesize = AsyncMock(return_value=b"OggSvoice")
    backend = ChatSessionCommsBackend(
        IncompleteHost(), manager, tts_provider_getter=MagicMock(), voice_synthesizer=synthesize
    )
    await backend.run_turn(_context(responder_config={"tts_enabled": True}))
    assert synthesize.await_count == 0
    assert manager.sent == []
    assert manager.attachments == []
