"""Telegram forum and private-topic routing tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.models import CommsAttachment, CommsMessage


def _payload(*, chat_type: str, thread_id: int | None) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": 1366,
        "from": {"id": 1111111, "username": "testuser"},
        "chat": {"id": 2222222, "type": chat_type},
        "text": "topic message",
    }
    if thread_id is not None:
        message["message_thread_id"] = thread_id
        message["is_topic_message"] = True
    return {"update_id": 10001, "message": message}


@pytest.mark.parametrize("chat_type", ["supergroup", "private"])
def test_parse_webhook_preserves_topic_identity(chat_type: str) -> None:
    adapter = TelegramAdapter()

    messages = adapter.parse_webhook(_payload(chat_type=chat_type, thread_id=42), {})

    assert len(messages) == 1
    message = messages[0]
    assert message.platform_thread_id == "42"
    assert message.metadata_json["message_thread_id"] == "42"
    assert message.metadata_json["is_topic_message"] is True


def test_parse_webhook_does_not_invent_thread_for_non_topic_dm() -> None:
    adapter = TelegramAdapter()

    message = adapter.parse_webhook(_payload(chat_type="private", thread_id=None), {})[0]

    assert message.platform_thread_id is None
    assert "message_thread_id" not in message.metadata_json
    assert "is_topic_message" not in message.metadata_json


@pytest.mark.asyncio
async def test_send_message_targets_topic_without_reply_to_message_id() -> None:
    adapter = TelegramAdapter()
    adapter._client = MagicMock()
    adapter._api_base = "https://api.telegram.org/bottest-token"
    message = CommsMessage(
        id="message-id",
        channel_id="channel-id",
        direction="outbound",
        content="topic reply",
        platform_thread_id="42",
        metadata_json={"platform_destination": "2222222"},
        created_at=datetime.now(UTC),
    )
    post_json = AsyncMock(return_value={"ok": True, "result": {"message_id": 99}})

    with patch.object(adapter, "_post_json", post_json):
        result = await adapter.send_message(message)

    assert result == "99"
    awaited_call = post_json.await_args
    assert awaited_call is not None
    payload = awaited_call.args[1]
    assert payload["message_thread_id"] == 42
    assert "reply_to_message_id" not in payload


@pytest.mark.asyncio
async def test_send_attachment_targets_topic_without_reply_to_message_id(
    tmp_path: Path,
) -> None:
    adapter = TelegramAdapter()
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://api.telegram.org/bottest-token/sendDocument"),
        json={"ok": True, "result": {"message_id": 99}},
    )
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=response)
    adapter._client = mock_client
    adapter._api_base = "https://api.telegram.org/bottest-token"
    file_path = tmp_path / "topic.txt"
    file_path.write_text("topic attachment")
    message = CommsMessage(
        id="message-id",
        channel_id="channel-id",
        direction="outbound",
        content="",
        platform_thread_id="42",
        metadata_json={"platform_destination": "2222222"},
        created_at=datetime.now(UTC),
    )
    attachment = CommsAttachment(
        id="attachment-id",
        message_id=message.id,
        filename=file_path.name,
        content_type="text/plain",
        size_bytes=file_path.stat().st_size,
    )

    result = await adapter.send_attachment(message, attachment, file_path)

    assert result == "99"
    data = mock_client.post.await_args.kwargs["data"]
    assert data["message_thread_id"] == 42
    assert "reply_to_message_id" not in data
