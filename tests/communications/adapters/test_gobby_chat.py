"""Tests for the Gobby Chat communications adapter."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from gobby.communications.adapters.gobby_chat import GobbyChatAdapter
from gobby.communications.models import ChannelConfig, CommsMessage


@pytest.fixture
def channel_config() -> ChannelConfig:
    return ChannelConfig(
        id="test_gobby_chat_channel",
        channel_type="gobby_chat",
        name="gobby_chat",
        enabled=True,
        config_json={},
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )


@pytest.fixture
def secret_resolver() -> Callable[[str], str | None]:
    def resolver(key: str) -> str | None:
        return None

    return resolver


@pytest.fixture
def adapter() -> GobbyChatAdapter:
    return GobbyChatAdapter()


@pytest.fixture
def initialized_adapter(
    adapter: GobbyChatAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> GobbyChatAdapter:
    """Return an adapter that has been synchronously initialized."""
    # Bypass async initialize for simple state setup
    adapter._initialized = True
    return adapter


@pytest.fixture
def outbound_message() -> CommsMessage:
    return CommsMessage(
        id="msg-001",
        channel_id="test_gobby_chat_channel",
        direction="outbound",
        content="Hello from GobbyChat!",
        created_at="2024-01-01T00:00:00Z",
        session_id="session-abc",
        content_type="text",
        metadata_json={"source_channel": "gobby_chat"},
    )


# --- Property tests ---


def test_channel_type(adapter: GobbyChatAdapter) -> None:
    assert adapter.channel_type == "gobby_chat"


def test_max_message_length(adapter: GobbyChatAdapter) -> None:
    assert adapter.max_message_length == 100_000


def test_supports_webhooks(adapter: GobbyChatAdapter) -> None:
    assert adapter.supports_webhooks is False


def test_supports_polling(adapter: GobbyChatAdapter) -> None:
    assert adapter.supports_polling is False


# --- Capabilities ---


def test_capabilities(adapter: GobbyChatAdapter) -> None:
    caps = adapter.capabilities()
    assert caps.threading is True
    assert caps.reactions is False
    assert caps.files is True
    assert caps.markdown is True
    assert caps.max_message_length == 100_000


# --- Initialize ---


@pytest.mark.asyncio
async def test_initialize(
    adapter: GobbyChatAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    await adapter.initialize(channel_config, secret_resolver)
    assert adapter._initialized is True


# --- Send message ---


@pytest.mark.asyncio
async def test_send_message_with_broadcast(
    initialized_adapter: GobbyChatAdapter,
    outbound_message: CommsMessage,
) -> None:
    mock_broadcast = AsyncMock()
    initialized_adapter.set_broadcast(mock_broadcast)

    result = await initialized_adapter.send_message(outbound_message)

    assert result == "msg-001"
    mock_broadcast.assert_called_once()
    payload = mock_broadcast.call_args[0][0]
    assert payload["type"] == "comms_message"
    assert payload["channel_type"] == "gobby_chat"
    assert payload["message_id"] == "msg-001"
    assert payload["content"] == "Hello from GobbyChat!"
    assert payload["session_id"] == "session-abc"
    assert payload["direction"] == "outbound"


@pytest.mark.asyncio
async def test_send_message_without_broadcast(
    initialized_adapter: GobbyChatAdapter,
    outbound_message: CommsMessage,
) -> None:
    """When no broadcast is set, message should still succeed but log a warning."""
    result = await initialized_adapter.send_message(outbound_message)
    assert result == "msg-001"


@pytest.mark.asyncio
async def test_send_message_not_initialized(
    adapter: GobbyChatAdapter,
    outbound_message: CommsMessage,
) -> None:
    with pytest.raises(RuntimeError, match="not initialized"):
        await adapter.send_message(outbound_message)


@pytest.mark.asyncio
async def test_send_message_broadcast_error(
    initialized_adapter: GobbyChatAdapter,
    outbound_message: CommsMessage,
) -> None:
    mock_broadcast = AsyncMock(side_effect=ConnectionError("ws gone"))
    initialized_adapter.set_broadcast(mock_broadcast)

    with pytest.raises(ConnectionError, match="ws gone"):
        await initialized_adapter.send_message(outbound_message)


# --- Shutdown ---


@pytest.mark.asyncio
async def test_shutdown(initialized_adapter: GobbyChatAdapter) -> None:
    mock_broadcast = AsyncMock()
    initialized_adapter.set_broadcast(mock_broadcast)

    await initialized_adapter.shutdown()

    assert initialized_adapter._broadcast is None
    assert initialized_adapter._initialized is False


# --- Webhook not supported ---


def test_parse_webhook_raises(adapter: GobbyChatAdapter) -> None:
    with pytest.raises(NotImplementedError, match="does not support webhooks"):
        adapter.parse_webhook({}, {})


def test_verify_webhook_raises(adapter: GobbyChatAdapter) -> None:
    with pytest.raises(NotImplementedError, match="does not support webhook verification"):
        adapter.verify_webhook(b"", {}, "secret")


# --- parse_inbound ---


def test_parse_inbound_valid(adapter: GobbyChatAdapter) -> None:
    data = {
        "type": "chat_message",
        "content": "Hello world",
        "conversation_id": "conv-123",
        "user_id": "user-456",
        "message_id": "msg-789",
        "model": "claude-sonnet-4-6",
    }
    msg = adapter.parse_inbound(data)

    assert msg is not None
    assert msg.content == "Hello world"
    assert msg.session_id == "conv-123"
    assert msg.identity_id == "user-456"
    assert msg.id == "msg-789"
    assert msg.channel_id == ""
    assert msg.metadata_json["platform_channel_id"] == "gobby_chat"
    assert msg.direction == "inbound"
    assert msg.content_type == "text"
    # model should be in metadata (not a core field)
    assert msg.metadata_json.get("model") == "claude-sonnet-4-6"


def test_parse_inbound_missing_content(adapter: GobbyChatAdapter) -> None:
    assert adapter.parse_inbound({"conversation_id": "conv-123"}) is None
    assert adapter.parse_inbound({"content": ""}) is None
    assert adapter.parse_inbound({"content": 123}) is None


def test_parse_inbound_generates_id(adapter: GobbyChatAdapter) -> None:
    msg = adapter.parse_inbound({"content": "test"})
    assert msg is not None
    assert msg.id  # Should have a generated UUID


# --- Registration ---


def test_adapter_registered() -> None:
    from gobby.communications.adapters import get_adapter_class

    cls = get_adapter_class("gobby_chat")
    assert cls is GobbyChatAdapter


# --- chunk_message (inherited) ---


def test_chunk_message_short(adapter: GobbyChatAdapter) -> None:
    chunks = adapter.chunk_message("short message")
    assert chunks == ["short message"]


def test_chunk_message_inherits_limit(adapter: GobbyChatAdapter) -> None:
    # Default limit is 100_000 so most messages won't be chunked
    long_msg = "a " * 60_000  # 120_000 chars
    chunks = adapter.chunk_message(long_msg)
    assert len(chunks) > 1
