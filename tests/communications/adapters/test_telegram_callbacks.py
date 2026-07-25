"""Telegram inline-keyboard and callback-query routing tests."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.identities import IdentityResolution
from gobby.communications.inbound import InboundCommunications
from gobby.communications.models import ChannelConfig, CommsIdentity, CommsMessage
from gobby.communications.telegram_callbacks import TelegramCallbackRegistry
from gobby.utils.datetime import utc_now


def _callback_payload(
    callback_data: str,
    *,
    chat_id: int = 2222222,
    thread_id: int | None = 42,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": 99,
        "chat": {"id": chat_id, "type": "private"},
    }
    if thread_id is not None:
        message["message_thread_id"] = thread_id
    return {
        "update_id": 10002,
        "callback_query": {
            "id": "callback-1",
            "from": {"id": 1111111, "username": "testuser"},
            "message": message,
            "data": callback_data,
        },
    }


def _registry(clock: list[float]) -> TelegramCallbackRegistry:
    tokens = iter(("token-1", "token-2", "token-3"))
    return TelegramCallbackRegistry(
        clock=lambda: clock[0],
        token_factory=lambda: next(tokens),
    )


def test_callback_registry_scopes_tokens_and_consumes_valid_selection() -> None:
    clock = [100.0]
    registry = _registry(clock)
    markup = registry.register_keyboard(
        [[{"text": "Approve", "value": "approve"}]],
        session_id="session-1",
        chat_id="2222222",
        thread_id="42",
        ttl_seconds=30,
    )
    callback_data = markup["inline_keyboard"][0][0]["callback_data"]

    wrong_chat = registry.resolve(callback_data, chat_id="999", thread_id="42")
    resolved = registry.resolve(callback_data, chat_id="2222222", thread_id="42")
    replayed = registry.resolve(callback_data, chat_id="2222222", thread_id="42")

    assert wrong_chat.status == "invalid"
    assert resolved.status == "ok"
    assert resolved.session_id == "session-1"
    assert resolved.value == "approve"
    assert replayed.status == "invalid"


def test_callback_registry_expires_and_bounds_keyboards() -> None:
    clock = [100.0]
    registry = _registry(clock)
    markup = registry.register_keyboard(
        [[{"text": "Now", "value": "now"}]],
        session_id="session-1",
        chat_id="2222222",
        thread_id=None,
        ttl_seconds=5,
    )
    callback_data = markup["inline_keyboard"][0][0]["callback_data"]
    clock[0] = 106.0

    assert registry.resolve(callback_data, chat_id="2222222", thread_id=None).status == "expired"
    with pytest.raises(ValueError, match="at most"):
        registry.register_keyboard(
            [[{"text": str(index), "value": str(index)} for index in range(9)]],
            session_id="session-1",
            chat_id="2222222",
            thread_id=None,
            ttl_seconds=5,
        )
    with pytest.raises(ValueError, match="capacity"):
        TelegramCallbackRegistry(max_entries=1).register_keyboard(
            [
                [
                    {"text": "Approve", "value": "approve"},
                    {"text": "Reject", "value": "reject"},
                ]
            ],
            session_id="session-1",
            chat_id="2222222",
            thread_id=None,
            ttl_seconds=5,
        )


@pytest.mark.asyncio
async def test_adapter_sends_keyboard_and_routes_callback_to_originating_session() -> None:
    clock = [100.0]
    adapter = TelegramAdapter()
    adapter._callback_registry = _registry(clock)
    adapter._client = MagicMock()
    adapter._api_base = "https://api.telegram.org/bottest-token"
    message = CommsMessage(
        id="message-id",
        channel_id="channel-id",
        direction="outbound",
        content="Proceed?",
        session_id="session-1",
        platform_thread_id="42",
        metadata_json={
            "platform_destination": "2222222",
            "inline_keyboard": [[{"text": "Approve", "value": "approve"}]],
            "callback_ttl_seconds": 30,
        },
        created_at=datetime.now(UTC),
    )
    post_json = AsyncMock(return_value={"ok": True, "result": {"message_id": 99}})

    with patch.object(adapter, "_post_json", post_json):
        await adapter.send_message(message)

    awaited_call = post_json.await_args
    assert awaited_call is not None
    sent_payload = awaited_call.args[1]
    callback_data = sent_payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"]

    callback = adapter.parse_webhook(_callback_payload(callback_data), {})[0]

    assert callback.content == "approve"
    assert callback.content_type == "callback"
    assert callback.session_id == "session-1"
    assert callback.platform_thread_id == "42"
    assert callback.metadata_json["callback_status"] == "ok"
    assert callback.metadata_json["mentioned"] is True

    post_json.reset_mock()
    with patch.object(adapter, "_post_json", post_json):
        await adapter.acknowledge_messages([callback])
    post_json.assert_awaited_once_with(
        "answerCallbackQuery",
        {"callback_query_id": "callback-1", "text": "Selection received."},
    )


@pytest.mark.asyncio
async def test_adapter_rejects_expired_callback_without_agent_content() -> None:
    clock = [100.0]
    adapter = TelegramAdapter()
    adapter._callback_registry = _registry(clock)
    markup = adapter._callback_registry.register_keyboard(
        [[{"text": "Approve", "value": "approve"}]],
        session_id="session-1",
        chat_id="2222222",
        thread_id="42",
        ttl_seconds=5,
    )
    callback_data = markup["inline_keyboard"][0][0]["callback_data"]
    clock[0] = 106.0

    callback = adapter.parse_webhook(_callback_payload(callback_data), {})[0]

    assert callback.content == ""
    assert callback.session_id is None
    assert callback.metadata_json["callback_status"] == "expired"

    post_json = AsyncMock(return_value={"ok": True, "result": True})
    with patch.object(adapter, "_post_json", post_json):
        await adapter.acknowledge_webhook_messages([callback])
    post_json.assert_awaited_once_with(
        "answerCallbackQuery",
        {"callback_query_id": "callback-1", "text": "This action has expired."},
    )


@pytest.mark.asyncio
async def test_inbound_callback_preserves_registry_session_after_identity_resolution() -> None:
    channel = ChannelConfig(
        id="channel-1",
        channel_type="telegram",
        name="telegram",
        enabled=True,
        config_json={},
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    identity = CommsIdentity(
        id="identity-1",
        channel_id=channel.id,
        external_user_id="1111111",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    manager = MagicMock()
    manager._channel_by_name = {"telegram": channel}
    adapter = MagicMock()
    adapter.download_inbound_attachments = AsyncMock(return_value=[])
    manager._adapters = {"telegram": adapter}
    manager.admit_inbound_message = AsyncMock(return_value=True)
    manager._store.get_message_by_platform_id.return_value = None
    manager._store.create_message.side_effect = lambda message: message
    manager._identity_manager.resolve_inbound_identity.return_value = IdentityResolution(
        identity=identity,
        session_id="identity-session",
    )
    manager.get_voice_transcriber.return_value = None
    manager.get_vision_extract_service.return_value = None
    manager.event_callback = None
    manager.reaction_handler = None
    callback = CommsMessage(
        id="callback-message",
        channel_id="",
        direction="inbound",
        content="approve",
        content_type="callback",
        platform_message_id="callback:callback-1",
        session_id="originating-session",
        identity_id="1111111",
        metadata_json={
            "chat_id": "2222222",
            "platform_channel_id": "2222222",
            "conversation_type": "private",
            "external_username": "testuser",
            "callback_status": "ok",
            "callback_session_id": "originating-session",
        },
        created_at=utc_now(),
    )

    handled = await InboundCommunications(manager).handle_messages("telegram", [callback])

    assert handled[0].session_id == "originating-session"
    assert handled[0].identity_id == "identity-1"
    assert handled[0].content == "approve"
    manager._identity_manager.resolve_inbound_identity.assert_called_once()
    manager._store.create_message.assert_called_once_with(callback)
    adapter.download_inbound_attachments.assert_awaited_once_with(
        callback,
        manager.attachment_manager,
    )
