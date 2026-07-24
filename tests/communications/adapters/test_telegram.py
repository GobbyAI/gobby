"""Tests for the Telegram communications adapter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.models import ChannelConfig, CommsMessage


@pytest.fixture
def channel_config() -> ChannelConfig:
    return ChannelConfig(
        id="test_telegram_channel",
        channel_type="telegram",
        name="Test Telegram",
        enabled=True,
        config_json={"bot_token": "$secret:TELEGRAM_BOT_TOKEN"},
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        webhook_secret="test_secret_token",
    )


@pytest.fixture
def secret_resolver() -> Callable[[str], str | None]:
    def resolver(key: str) -> str | None:
        if key == "TELEGRAM_BOT_TOKEN":
            return "test-telegram-token"
        return None

    return resolver


@pytest.fixture
def adapter() -> TelegramAdapter:
    return TelegramAdapter()


@pytest.mark.asyncio
async def test_initialize_success(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    mock_post = AsyncMock()
    mock_post.return_value.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.post = mock_post

        # Test without webhook
        await adapter.initialize(channel_config, secret_resolver)
        assert adapter._bot_token == "test-telegram-token"
        assert adapter._api_base == "https://api.telegram.org/bottest-telegram-token"
        mock_post.assert_called_with(
            "https://api.telegram.org/bottest-telegram-token/deleteWebhook"
        )


@pytest.mark.asyncio
async def test_initialize_with_webhook(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    channel_config.config_json["webhook_base_url"] = "https://example.com/webhooks"

    mock_post = AsyncMock()
    mock_post.return_value.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.post = mock_post

        await adapter.initialize(channel_config, secret_resolver)

        mock_post.assert_called_with(
            "https://api.telegram.org/bottest-telegram-token/setWebhook",
            json={
                "url": f"https://example.com/webhooks/api/comms/webhooks/{channel_config.name}",
                "secret_token": "test_secret_token",
            },
        )
        assert mock_post.call_count >= 1
        assert mock_post.call_args is not None


@pytest.mark.asyncio
async def test_initialize_missing_token(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    channel_config.config_json = {}
    with pytest.raises(ValueError, match="Telegram bot_token not found"):
        await adapter.initialize(channel_config, secret_resolver)


@pytest.mark.asyncio
async def test_send_message_basic(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    mock_post = AsyncMock()

    # Mock behavior depending on the url called
    async def side_effect(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        if "deleteWebhook" in url:
            resp.json.return_value = {"ok": True}
        else:
            resp.json.return_value = {"ok": True, "result": {"message_id": 12345}}
        return resp

    mock_post.side_effect = side_effect

    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.post = mock_post

        await adapter.initialize(channel_config, secret_resolver)

        message = CommsMessage(
            id="msg1",
            channel_id=channel_config.id,
            direction="outbound",
            content="Hello world",
            platform_thread_id="reply123",
            metadata_json={"platform_destination": "chat999"},
            created_at=datetime.now(UTC).isoformat(),
        )

        msg_id = await adapter.send_message(message)

        assert message.channel_id != message.metadata_json["platform_destination"]
        assert msg_id == "12345"

        # Checking last call
        call_args, call_kwargs = mock_post.call_args_list[-1]
        assert call_args[0] == "https://api.telegram.org/bottest-telegram-token/sendMessage"
        assert call_kwargs["json"] == {
            "chat_id": "chat999",
            "text": "Hello world",
            "reply_to_message_id": "reply123",
        }


@pytest.mark.asyncio
async def test_send_message_http_status_error_redacts_bot_token(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    token = "test-telegram-token"
    mock_post = AsyncMock()

    async def side_effect(url, **kwargs):
        request = httpx.Request("POST", url)
        if "deleteWebhook" in url:
            return httpx.Response(200, request=request, json={"ok": True})
        return httpx.Response(400, request=request, json={"ok": False})

    mock_post.side_effect = side_effect

    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.post = mock_post

        await adapter.initialize(channel_config, secret_resolver)

        message = CommsMessage(
            id="msg1",
            channel_id=channel_config.id,
            direction="outbound",
            content="Hello world",
            metadata_json={"platform_destination": "chat999"},
            created_at=datetime.now(UTC).isoformat(),
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await adapter.send_message(message)

    exc = exc_info.value
    assert exc.response.status_code == 400
    assert token not in str(exc)
    assert token not in str(exc.request.url)
    assert "***" in str(exc)


@pytest.mark.asyncio
async def test_send_message_chunking(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    mock_post = AsyncMock()

    # Mock behavior depending on the url called
    async def side_effect(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        if "deleteWebhook" in url:
            resp.json.return_value = {"ok": True}
        else:
            resp.json.return_value = {"ok": True, "result": {"message_id": 999}}
        return resp

    mock_post.side_effect = side_effect

    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.post = mock_post

        await adapter.initialize(channel_config, secret_resolver)

        # Reset mock to only count send_message calls
        mock_post.reset_mock()
        mock_post.side_effect = side_effect  # Re-apply after reset

        long_content = "A" * 5000
        message = CommsMessage(
            id="msg1",
            channel_id=channel_config.id,
            direction="outbound",
            content=long_content,
            platform_thread_id="reply123",
            metadata_json={"platform_destination": "chat999"},
            created_at=datetime.now(UTC).isoformat(),
        )

        await adapter.send_message(message)

        assert mock_post.call_count == 2
        # First call with 4096 chars
        first_call_args = mock_post.call_args_list[0][1]
        assert len(first_call_args["json"]["text"]) == 4096
        # Second call with 904 chars
        second_call_args = mock_post.call_args_list[1][1]
        assert len(second_call_args["json"]["text"]) == 904


def test_parse_webhook(adapter: TelegramAdapter) -> None:
    payload = {
        "update_id": 10000,
        "message": {
            "message_id": 1365,
            "from": {"id": 1111111, "is_bot": False, "first_name": "Test", "username": "testuser"},
            "chat": {
                "id": 2222222,
                "first_name": "Test",
                "username": "testuser",
                "type": "private",
            },
            "date": 1441645532,
            "text": "/start",
        },
    }

    messages = adapter.parse_webhook(payload, {})
    assert len(messages) == 1

    msg = messages[0]
    assert msg.direction == "inbound"
    assert msg.content == "/start"
    assert msg.platform_message_id == "1365"
    assert msg.platform_thread_id == "1365"
    assert msg.metadata_json["user_id"] == "1111111"
    assert msg.metadata_json["username"] == "testuser"
    assert msg.metadata_json["chat_id"] == "2222222"
    assert msg.metadata_json["platform_channel_id"] == "2222222"
    assert msg.metadata_json["conversation_reference"] == {
        "conversation_id": "2222222",
    }


def test_parse_group_webhook_sets_conversation_reference(adapter: TelegramAdapter) -> None:
    payload = {
        "update_id": 10001,
        "message": {
            "message_id": 1366,
            "from": {"id": 1111111, "is_bot": False, "username": "testuser"},
            "chat": {
                "id": -1002222222,
                "title": "Test group",
                "type": "supergroup",
            },
            "date": 1441645532,
            "text": "hello group",
        },
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    assert messages[0].metadata_json["conversation_reference"] == {
        "conversation_id": "-1002222222",
    }


def test_verify_webhook(adapter: TelegramAdapter) -> None:
    assert (
        adapter.verify_webhook(b"", {"x-telegram-bot-api-secret-token": "secret123"}, "secret123")
        is True
    )
    assert (
        adapter.verify_webhook(b"", {"x-telegram-bot-api-secret-token": "wrong"}, "secret123")
        is False
    )
    assert adapter.verify_webhook(b"", {}, "secret123") is False


@pytest.mark.asyncio
async def test_poll(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    mock_get = AsyncMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "ok": True,
        "result": [
            {
                "update_id": 500,
                "message": {
                    "message_id": 1,
                    "from": {"id": 1111111, "username": "polluser"},
                    "chat": {"id": 123},
                    "text": "hello",
                },
            }
        ],
    }
    mock_get.return_value = mock_response

    mock_post = AsyncMock()

    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.get = mock_get
        mock_client_instance.post = mock_post

        await adapter.initialize(channel_config, secret_resolver)

        assert adapter._offset == 0
        messages = await adapter.poll()

        assert len(messages) == 1
        assert messages[0].content == "hello"
        assert adapter._offset == 0
        await adapter.acknowledge_messages(messages)
        assert adapter._offset == 501

        mock_get.assert_called_with(
            "https://api.telegram.org/bottest-telegram-token/getUpdates",
            params={"offset": 0, "timeout": 30},
        )


@pytest.mark.asyncio
async def test_poll_acknowledges_only_contiguous_successful_updates(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    mock_get = AsyncMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "ok": True,
        "result": [
            {
                "update_id": 500,
                "message": {
                    "message_id": 1,
                    "from": {"id": 1111111, "username": "first"},
                    "chat": {"id": 123},
                    "text": "first",
                },
            },
            {
                "update_id": 501,
                "message": {
                    "message_id": 2,
                    "from": {"id": 2222222, "username": "second"},
                    "chat": {"id": 123},
                    "text": "second",
                },
            },
        ],
    }
    mock_get.return_value = mock_response

    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.get = mock_get
        mock_client_instance.post = AsyncMock()

        await adapter.initialize(channel_config, secret_resolver)
        messages = await adapter.poll()

        await adapter.acknowledge_messages([messages[1]])
        assert adapter._offset == 0

        await adapter.acknowledge_messages([messages[0]])
        assert adapter._offset == 502


@pytest.mark.asyncio
async def test_poll_ignores_non_message_updates_for_offset_tracking(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    mock_get = AsyncMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "ok": True,
        "result": [
            {"update_id": 500, "edited_message": {"message_id": 1}},
            {
                "update_id": 501,
                "message": {
                    "message_id": 2,
                    "from": {"id": 2222222, "username": "second"},
                    "chat": {"id": 123},
                    "text": "second",
                },
            },
        ],
    }
    mock_get.return_value = mock_response

    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.get = mock_get
        mock_client_instance.post = AsyncMock()

        await adapter.initialize(channel_config, secret_resolver)
        messages = await adapter.poll()

        assert [message.content for message in messages] == ["second"]

        await adapter.acknowledge_messages(messages)
        assert adapter._offset == 502


@pytest.mark.asyncio
async def test_poll_offset_restores_and_persists_after_acknowledgement(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    channel_config.config_json["poll_offset"] = 500
    persist_config = AsyncMock()
    adapter.set_config_update_callback(persist_config)

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.post = AsyncMock()
        await adapter.initialize(channel_config, secret_resolver)

    assert adapter._offset == 500

    adapter._pending_update_ids = [500]
    message = CommsMessage(
        id="message-500",
        channel_id=channel_config.id,
        direction="inbound",
        content="hello",
        metadata_json={"telegram_update_id": 500},
        created_at=datetime.now(UTC),
    )

    await adapter.acknowledge_messages([message])

    assert adapter._offset == 501
    persist_config.assert_awaited_once_with({"poll_offset": 501})


async def test_shutdown(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    mock_aclose = AsyncMock()
    mock_post = AsyncMock()

    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.aclose = mock_aclose
        mock_client_instance.post = mock_post

        await adapter.initialize(channel_config, secret_resolver)
        await adapter.shutdown()

        mock_aclose.assert_called_once()
        assert adapter._client is None
