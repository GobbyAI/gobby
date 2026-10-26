"""Persisted Telegram delivery through the responder and attached transcript paths."""

import json
from dataclasses import replace
from unittest.mock import MagicMock

import httpx
import pytest

from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.chat_transport import CommunicationsChatStreamTransport
from gobby.communications.manager import CommunicationsManager
from gobby.communications.models import ChannelConfig, CommsIdentity
from gobby.config.communications import CommunicationsConfig
from gobby.storage.communications import LocalCommunicationsStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.datetime import utc_now
from tests.communications.test_chat_transport import _context
from tests.sessions.test_transcript_mirror import processor, record

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("attached", [False, True])
async def test_completion_delivers_one_whole_reply_with_live_attribution(
    temp_db: HubDatabase, sample_project: dict[str, str], attached: bool
) -> None:
    sessions = SessionManager(temp_db)
    expired = sessions.register("old-assistant", None, "claude", sample_project["id"])
    sessions.update_status(expired.id, "expired")
    live = sessions.register(
        "live-assistant" if attached else "live-responder",
        None,
        "claude",
        sample_project["id"],
        session_type="terminal" if attached else "web_chat",
    )
    store = LocalCommunicationsStore(temp_db)
    now = utc_now()
    channel = store.create_channel(
        ChannelConfig(
            id="",
            name="telegram",
            channel_type="telegram",
            enabled=True,
            config_json={"operator_project_id": sample_project["id"]},
            created_at=now,
            updated_at=now,
        )
    )
    store.create_identity(
        CommsIdentity(
            id="",
            channel_id=channel.id,
            external_user_id="42",
            session_id=expired.id,
            created_at=now,
            updated_at=now,
        )
    )
    manager = CommunicationsManager(CommunicationsConfig(), store, MagicMock(), sessions)
    manager._channel_by_name[channel.name] = channel
    if attached:
        manager.attach_conversation(channel.name, "dm:42", live.id)
    context = replace(_context(session_id=expired.id), channel=channel, conversation_id="42")
    payloads: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(payloads)}})

    reply = "x" * 9000
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = TelegramAdapter()
        adapter._client = client
        adapter._api_base = "https://telegram.test"
        manager._adapters[channel.name] = adapter
        transport = CommunicationsChatStreamTransport(manager, context)
        await transport.send_direct({"type": "session_info", "db_session_id": live.id})
        await transport.safe_send({"type": "chat_stream", "content": reply[:6]})
        assert store.list_messages(channel_id=channel.id) == []
        await transport.safe_send({"type": "chat_stream", "content": reply[6:], "done": True})
        await transport.safe_send({"type": "chat_stream", "done": True})
        await transport.finalize()
        batch = [record(0, "assistant", reply), record(1, "user", "next")]
        await processor(temp_db, manager)._process_parsed_batch(live.id, batch)
        await processor(temp_db, manager)._process_parsed_batch(live.id, batch)

    rows = store.list_messages(channel_id=channel.id)
    assert [(row.content, row.session_id, row.status) for row in rows] == [(reply, live.id, "sent")]
    label = f"<b>Claude ({live.ref}):</b>\n"
    assert "".join(str(payload["text"]).removeprefix(label) for payload in payloads) == reply
    assert all(payload["chat_id"] == "42" for payload in payloads)
