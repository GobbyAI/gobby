from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.communications.adapters.discord import DiscordAdapter
from gobby.communications.models import ChannelConfig, CommsMessage


@pytest.fixture
def adapter():
    return DiscordAdapter()


@pytest.fixture
def mock_secret_resolver():
    def _resolve(secret_ref: str) -> str | None:
        if secret_ref == "$secret:DISCORD_BOT_TOKEN":
            return "test_token"
        return "direct_token" if not secret_ref.startswith("$secret:") else None

    return _resolve


@pytest.mark.asyncio
async def test_initialize(adapter, mock_secret_resolver):
    config = ChannelConfig(
        id="test",
        channel_type="discord",
        name="test",
        enabled=True,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        config_json={"bot_token": "$secret:DISCORD_BOT_TOKEN", "enable_gateway": False},
    )
    await adapter.initialize(config, mock_secret_resolver)

    assert adapter._bot_token == "test_token"
    assert adapter._client is not None
    assert "Authorization" in adapter._client.headers
    assert adapter._client.headers["Authorization"] == "Bot test_token"


@pytest.mark.asyncio
async def test_send_message(adapter, mock_secret_resolver):
    config = ChannelConfig(
        id="test",
        channel_type="discord",
        name="test",
        enabled=True,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        config_json={
            "bot_token": "$secret:DISCORD_BOT_TOKEN",
            "enable_gateway": False,
            "channel_id": "channel_123",
        },
    )
    await adapter.initialize(config, mock_secret_resolver)

    msg = CommsMessage(
        id="test_id",
        channel_id="gobby-internal-channel",
        direction="outbound",
        content="Hello world",
        metadata_json={"platform_destination": "channel_123"},
        created_at="2024-01-01T00:00:00Z",
    )

    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.json.return_value = {"id": "msg_456"}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        result = await adapter.send_message(msg)

        assert result == "msg_456"
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "/channels/channel_123/messages"
        assert kwargs["json"]["content"] == "Hello world"


@pytest.mark.asyncio
async def test_send_message_chunking(adapter, mock_secret_resolver):
    config = ChannelConfig(
        id="test",
        channel_type="discord",
        name="test",
        enabled=True,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        config_json={
            "bot_token": "$secret:DISCORD_BOT_TOKEN",
            "enable_gateway": False,
            "channel_id": "channel_123",
        },
    )
    await adapter.initialize(config, mock_secret_resolver)

    long_content = "A" * 2500
    msg = CommsMessage(
        id="test_id",
        channel_id="gobby-internal-channel",
        direction="outbound",
        content=long_content,
        metadata_json={"platform_destination": "channel_123"},
        created_at="2024-01-01T00:00:00Z",
    )

    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.json.return_value = {"id": "msg_456"}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        result = await adapter.send_message(msg)

        assert mock_post.call_count == 2
        assert result == "msg_456"


def test_parse_webhook(adapter):
    payload = {
        "type": 0,
        "channel_id": "channel_123",
        "id": "msg_123",
        "author": {"id": "user_123"},
        "content": "Hello discord",
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    assert messages[0].channel_id == ""
    assert messages[0].metadata_json["platform_channel_id"] == "channel_123"
    assert messages[0].content == "Hello discord"
    assert messages[0].identity_id == "user_123"


def test_parse_webhook_reaction_added(adapter):
    """parse_webhook() parses MESSAGE_REACTION_ADD into reaction CommsMessage."""
    payload = {
        "t": "MESSAGE_REACTION_ADD",
        "d": {
            "user_id": "user_789",
            "channel_id": "channel_123",
            "message_id": "msg_456",
            "emoji": {"id": None, "name": "thumbsup"},
        },
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    msg = messages[0]
    assert msg.content_type == "reaction"
    assert msg.content == "thumbsup"
    assert msg.platform_message_id == "msg_456"
    assert msg.identity_id == "user_789"
    assert msg.channel_id == ""
    assert msg.metadata_json["platform_channel_id"] == "channel_123"


def test_parse_webhook_extracts_thread_id(adapter):
    """parse_webhook() extracts platform_thread_id from thread metadata."""
    payload = {
        "type": 0,
        "channel_id": "channel_123",
        "id": "msg_123",
        "author": {"id": "user_123"},
        "content": "Reply in thread",
        "thread": {"id": "thread_999"},
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    assert messages[0].platform_thread_id == "thread_999"


def test_parse_webhook_extracts_thread_from_message_reference(adapter):
    """parse_webhook() extracts thread from message_reference when no thread metadata."""
    payload = {
        "type": 0,
        "channel_id": "channel_123",
        "id": "msg_123",
        "author": {"id": "user_123"},
        "content": "Reply via reference",
        "message_reference": {"channel_id": "thread_888", "message_id": "msg_original"},
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    assert messages[0].platform_thread_id == "thread_888"


def test_verify_webhook(adapter):
    # This requires ed25519 keys to properly test, we'll test the failure cases or mock
    # since cryptography is optional

    result = adapter.verify_webhook(
        b"payload", {"X-Signature-Ed25519": "invalid", "X-Signature-Timestamp": "123"}, "secret"
    )
    assert not result

    result = adapter.verify_webhook(b"payload", {}, "secret")
    assert not result
