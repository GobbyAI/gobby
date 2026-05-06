"""Tests for threading support in CommunicationsManager."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.communications.manager import CommunicationsManager
from gobby.communications.models import ChannelConfig, CommsIdentity, CommsMessage
from gobby.config.communications import ChannelDefaults, CommunicationsConfig


def _config() -> CommunicationsConfig:
    return CommunicationsConfig(
        enabled=True,
        channel_defaults=ChannelDefaults(rate_limit_per_minute=60, burst=10),
    )


def _channel(
    name: str = "test-channel",
    channel_id: str = "chan-1",
) -> ChannelConfig:
    return ChannelConfig(
        id=channel_id,
        channel_type="test",
        name=name,
        enabled=True,
        config_json={},
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
    )


def _store(channels: list[ChannelConfig] | None = None) -> MagicMock:
    store = MagicMock()
    store.list_channels.return_value = channels or []
    store.get_routing_rules.return_value = []
    store.create_message.return_value = None
    store.get_identity_by_external.return_value = None
    return store


def _adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.channel_type = "test"
    adapter.supports_webhooks = True
    adapter.supports_polling = False
    adapter.initialize = AsyncMock()
    adapter.send_message = AsyncMock(return_value="platform-msg-1")
    adapter.shutdown = AsyncMock()
    adapter.parse_webhook.return_value = []
    adapter.verify_webhook.return_value = True
    return adapter


async def _make_manager(store: MagicMock) -> CommunicationsManager:
    manager = CommunicationsManager(_config(), store, MagicMock(), MagicMock())
    mock_adapter = _adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)
    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()
    return manager


# --- Thread map population from inbound messages ---


@pytest.mark.asyncio
async def test_inbound_populates_thread_map():
    """Inbound message with session_id and platform_thread_id populates thread map."""
    channel = _channel()
    store = _store([channel])

    identity = CommsIdentity(
        id="id-1",
        channel_id="chan-1",
        external_user_id="ext-user-1",
        session_id="session-123",
        created_at="",
        updated_at="",
    )
    store.get_identity_by_external.return_value = identity

    manager = await _make_manager(store)

    msg = CommsMessage(
        id="msg-1",
        channel_id="chan-1",
        direction="inbound",
        content="Hello",
        platform_thread_id="thread-abc",
        created_at="",
        identity_id="ext-user-1",
    )

    await manager.handle_inbound_messages("test-channel", [msg])

    assert manager._thread_manager._thread_map[("test-channel", "session-123")] == "thread-abc"


@pytest.mark.asyncio
async def test_inbound_without_thread_id_does_not_populate():
    """Inbound message without platform_thread_id does not populate thread map."""
    channel = _channel()
    store = _store([channel])

    identity = CommsIdentity(
        id="id-1",
        channel_id="chan-1",
        external_user_id="ext-user-1",
        session_id="session-123",
        created_at="",
        updated_at="",
    )
    store.get_identity_by_external.return_value = identity

    manager = await _make_manager(store)

    msg = CommsMessage(
        id="msg-1",
        channel_id="chan-1",
        direction="inbound",
        content="Hello",
        platform_thread_id=None,
        created_at="",
        identity_id="ext-user-1",
    )

    await manager.handle_inbound_messages("test-channel", [msg])

    assert ("test-channel", "session-123") not in manager._thread_manager._thread_map


@pytest.mark.asyncio
async def test_inbound_without_session_does_not_populate():
    """Inbound message without session_id does not populate thread map."""
    channel = _channel()
    store = _store([channel])
    # No identity found -> no session_id
    store.get_identity_by_external.return_value = None

    manager = await _make_manager(store)

    msg = CommsMessage(
        id="msg-1",
        channel_id="chan-1",
        direction="inbound",
        content="Hello",
        platform_thread_id="thread-abc",
        created_at="",
    )

    await manager.handle_inbound_messages("test-channel", [msg])

    assert len(manager._thread_manager._thread_map) == 0


# --- Thread map updates on new thread ---


@pytest.mark.asyncio
async def test_thread_map_updates_on_new_thread():
    """Thread map is overwritten when a newer thread arrives for same session+channel."""
    channel = _channel()
    store = _store([channel])

    identity = CommsIdentity(
        id="id-1",
        channel_id="chan-1",
        external_user_id="ext-user-1",
        session_id="session-123",
        created_at="",
        updated_at="",
    )
    store.get_identity_by_external.return_value = identity

    manager = await _make_manager(store)

    msg1 = CommsMessage(
        id="msg-1",
        channel_id="chan-1",
        direction="inbound",
        content="First",
        platform_thread_id="thread-old",
        created_at="",
        identity_id="ext-user-1",
    )
    msg2 = CommsMessage(
        id="msg-2",
        channel_id="chan-1",
        direction="inbound",
        content="Second",
        platform_thread_id="thread-new",
        created_at="",
        identity_id="ext-user-1",
    )

    await manager.handle_inbound_messages("test-channel", [msg1, msg2])

    assert manager._thread_manager._thread_map[("test-channel", "session-123")] == "thread-new"


# --- Outbound thread propagation ---


@pytest.mark.asyncio
async def test_send_message_includes_thread_from_map():
    """send_message includes platform_thread_id from thread map when session matches."""
    channel = _channel()
    store = _store([channel])
    manager = await _make_manager(store)

    manager._thread_manager._thread_map[("test-channel", "session-123")] = "thread-456"

    msg = await manager.send_message("test-channel", "Reply", session_id="session-123")

    assert msg.platform_thread_id == "thread-456"


@pytest.mark.asyncio
async def test_send_message_no_thread_without_session():
    """send_message does not set thread_id when no session_id is provided."""
    channel = _channel()
    store = _store([channel])
    manager = await _make_manager(store)

    manager._thread_manager._thread_map[("test-channel", "session-123")] = "thread-456"

    msg = await manager.send_message("test-channel", "Broadcast")

    assert msg.platform_thread_id is None


@pytest.mark.asyncio
async def test_send_message_no_thread_for_unknown_session():
    """send_message does not set thread_id when session has no thread mapping."""
    channel = _channel()
    store = _store([channel])
    manager = await _make_manager(store)

    msg = await manager.send_message("test-channel", "Hello", session_id="unknown-session")

    assert msg.platform_thread_id is None


# --- Thread isolation per channel ---


@pytest.mark.asyncio
async def test_thread_map_isolated_per_channel():
    """Thread map entries are scoped to channel:session, not just session."""
    ch1 = _channel(name="slack", channel_id="chan-slack")
    ch2 = _channel(name="discord", channel_id="chan-discord")
    store = _store([ch1, ch2])

    manager = CommunicationsManager(_config(), store, MagicMock(), MagicMock())

    mock_adapter = _adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)
    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    # Directly populate thread map for two different channels, same session
    manager._thread_manager._thread_map[("slack", "session-1")] = "slack-thread-ts"
    manager._thread_manager._thread_map[("discord", "session-1")] = "discord-thread-id"

    # Send to each channel
    msg_slack = await manager.send_message("slack", "Reply", session_id="session-1")
    msg_discord = await manager.send_message("discord", "Reply", session_id="session-1")

    assert msg_slack.platform_thread_id == "slack-thread-ts"
    assert msg_discord.platform_thread_id == "discord-thread-id"


# --- Adapter-level threading (Slack uses thread_ts) ---


@pytest.mark.asyncio
async def test_slack_adapter_sends_with_thread_ts():
    """Slack adapter sends messages with thread_ts from CommsMessage.platform_thread_id."""
    from gobby.communications.adapters.slack import SlackAdapter

    adapter = SlackAdapter()
    adapter._client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.json.return_value = {"ok": True, "ts": "1234567890.111111"}
    mock_response.raise_for_status = MagicMock()
    adapter._client.post = AsyncMock(return_value=mock_response)

    msg = CommsMessage(
        id="msg-1",
        channel_id="C123",
        direction="outbound",
        content="Thread reply",
        platform_thread_id="1234567890.000001",
        created_at="2024-01-01",
        metadata_json={"platform_destination": "C123"},
    )

    await adapter.send_message(msg)

    call_kwargs = adapter._client.post.call_args
    json_body = call_kwargs.kwargs.get("json", {})
    # The Slack adapter should include thread_ts in the payload
    assert json_body.get("thread_ts") == "1234567890.000001"
    assert json_body["text"] == "Thread reply"


# --- Discord adapter threading ---


@pytest.mark.asyncio
async def test_discord_adapter_thread_extraction():
    """Discord adapter extracts thread_id from webhook metadata."""
    from gobby.communications.adapters.discord import DiscordAdapter

    adapter = DiscordAdapter()

    payload = {
        "type": 0,
        "content": "Hello from thread",
        "id": "msg-discord-1",
        "author": {"id": "user-1", "username": "alice"},
        "channel_id": "thread-channel-1",
        "message_reference": {"message_id": "parent-msg-1", "channel_id": "thread-channel-1"},
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    assert messages[0].platform_thread_id == "thread-channel-1"
