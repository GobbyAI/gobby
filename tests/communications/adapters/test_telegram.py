"""Tests for the Telegram communications adapter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

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
                "url": "https://example.com/webhooks/v1/comms/webhooks/test_telegram_channel",
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
            metadata_json={"chat_id": "chat999"},
            created_at=datetime.now(UTC).isoformat(),
        )

        msg_id = await adapter.send_message(message)

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
            metadata_json={"chat_id": "chat999"},
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
                "message": {"message_id": 1, "chat": {"id": 123}, "text": "hello"},
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
        assert adapter._offset == 501

        mock_get.assert_called_with(
            "https://api.telegram.org/bottest-telegram-token/getUpdates",
            params={"offset": 0, "timeout": 30},
        )


@pytest.mark.asyncio
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
