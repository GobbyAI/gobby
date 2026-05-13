"""Tests for CommunicationsManager."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.communications.manager import CommunicationsManager
from gobby.communications.models import ChannelConfig, CommsIdentity, CommsMessage
from gobby.config.communications import ChannelDefaults, CommunicationsConfig


def make_config() -> CommunicationsConfig:
    return CommunicationsConfig(
        enabled=True,
        channel_defaults=ChannelDefaults(rate_limit_per_minute=60, burst=10),
    )


def make_channel(
    name: str = "test-channel",
    channel_type: str = "test",
    channel_id: str = "chan-1",
    enabled: bool = True,
    config_json: dict | None = None,
    webhook_secret: str | None = None,
) -> ChannelConfig:
    return ChannelConfig(
        id=channel_id,
        channel_type=channel_type,
        name=name,
        enabled=enabled,
        config_json=config_json or {},
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
        webhook_secret=webhook_secret,
    )


def make_store(channels: list[ChannelConfig] | None = None) -> MagicMock:
    store = MagicMock()
    store.list_channels.return_value = channels or []
    store.get_routing_rules.return_value = []
    store.create_message.return_value = None
    store.create_channel.return_value = None
    store.delete_channel.return_value = None
    store.get_identity_by_external.return_value = None
    return store


def make_secret_store() -> MagicMock:
    secret_store = MagicMock()
    secret_store.get.return_value = None
    return secret_store


def make_adapter(
    channel_type: str = "test",
    supports_webhooks: bool = True,
    supports_polling: bool = False,
) -> MagicMock:
    adapter = MagicMock()
    adapter.channel_type = channel_type
    adapter.supports_webhooks = supports_webhooks
    adapter.supports_polling = supports_polling
    adapter.initialize = AsyncMock()
    adapter.send_message = AsyncMock(return_value="platform-msg-id-1")
    adapter.shutdown = AsyncMock()
    adapter.parse_webhook.return_value = []
    adapter.verify_webhook.return_value = True
    return adapter


@pytest.mark.asyncio
async def test_start_loads_channels():
    """start() loads enabled channels and initializes adapters."""
    channel = make_channel()
    store = make_store([channel])
    secret_store = make_secret_store()
    config = make_config()

    manager = CommunicationsManager(config, store, secret_store, MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    assert "test-channel" in manager._adapters
    mock_adapter.initialize.assert_called_once()
    # start() calls list_channels twice: once for _ensure_gobby_chat_channel (enabled_only=False)
    # and once for loading active channels (enabled_only=True)
    assert store.list_channels.call_count == 2
    store.list_channels.assert_any_call(enabled_only=False)
    store.list_channels.assert_any_call(enabled_only=True)


@pytest.mark.asyncio
async def test_start_skips_unknown_adapter():
    """start() logs error but continues if adapter type is unknown."""
    channel = make_channel(channel_type="unknown_type")
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    with patch("gobby.communications.manager.get_adapter_class", return_value=None):
        await manager.start()

    assert "test-channel" not in manager._adapters


@pytest.mark.asyncio
async def test_stop_shuts_down_all_adapters():
    """stop() calls shutdown on all active adapters and clears state."""
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    await manager.stop()

    mock_adapter.shutdown.assert_called_once()
    assert len(manager._adapters) == 0
    assert len(manager._channel_by_name) == 0


@pytest.mark.asyncio
async def test_send_message_success():
    """send_message() sends and stores message, returns CommsMessage."""
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    msg = await manager.send_message("test-channel", "Hello!")

    assert msg.content == "Hello!"
    assert msg.direction == "outbound"
    assert msg.status == "sent"
    assert msg.platform_message_id == "platform-msg-id-1"
    mock_adapter.send_message.assert_called_once()
    store.create_message.assert_called_once()


@pytest.mark.asyncio
async def test_send_message_unknown_channel_raises():
    """send_message() raises ValueError for unknown channel."""
    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    with pytest.raises(ValueError, match="not found or not active"):
        await manager.send_message("no-such-channel", "Hello!")


@pytest.mark.asyncio
async def test_send_message_adapter_failure_marks_failed():
    """send_message() marks message failed if adapter raises."""
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter.send_message = AsyncMock(side_effect=RuntimeError("network error"))
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    msg = await manager.send_message("test-channel", "Hello!")

    assert msg.status == "failed"
    assert "network error" in (msg.error or "")
    store.create_message.assert_called_once()


@pytest.mark.asyncio
async def test_send_message_fires_event_callback():
    """send_message() fires event_callback after send."""
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    callback_events = []

    async def cb(event_type: str, **kwargs: Any) -> None:
        callback_events.append((event_type, kwargs))

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    manager.event_callback = cb
    await manager.send_message("test-channel", "Hello!")

    assert len(callback_events) == 1
    assert callback_events[0][0] == "comms.message_sent"


@pytest.mark.asyncio
async def test_send_event_routes_to_channels():
    """send_event() uses router to find channels and sends to each."""
    channel = make_channel(channel_id="chan-1")
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    # Mock router to return our channel id
    manager._router.match_channels = AsyncMock(return_value=["chan-1"])

    msgs = await manager.send_event("task.created", "A task was created!")

    assert len(msgs) == 1
    assert msgs[0].content == "A task was created!"


@pytest.mark.asyncio
async def test_send_event_skips_inactive_channels():
    """send_event() skips channel IDs that don't have active adapters."""
    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    manager._router.match_channels = AsyncMock(return_value=["chan-inactive"])

    msgs = await manager.send_event("task.created", "Hello!")
    assert msgs == []


@pytest.mark.asyncio
async def test_handle_inbound_stores_messages():
    """handle_inbound() parses and stores messages."""
    channel = make_channel(webhook_secret=None)
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    parsed_msg = CommsMessage(
        id="msg-1",
        channel_id="chan-1",
        direction="inbound",
        content="Hi there!",
        created_at="2024-01-01T00:00:00",
    )

    mock_adapter = make_adapter()
    mock_adapter.parse_webhook.return_value = [parsed_msg]
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    stored = await manager.handle_inbound("test-channel", {"data": "payload"}, {})

    assert len(stored) == 1
    assert stored[0].content == "Hi there!"
    store.create_message.assert_called_once()


@pytest.mark.asyncio
async def test_handle_inbound_webhook_verification_failure():
    """handle_inbound() raises ValueError if webhook signature fails."""
    channel = make_channel(webhook_secret="mysecret")
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter.verify_webhook.return_value = False
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    with pytest.raises(ValueError, match="signature verification failed"):
        await manager.handle_inbound("test-channel", b"payload", {"X-Signature": "bad"})


@pytest.mark.asyncio
async def test_handle_inbound_resolves_identity():
    """handle_inbound() resolves identity and sets session_id."""
    channel = make_channel()
    store = make_store([channel])

    identity = CommsIdentity(
        id="identity-1",
        channel_id="chan-1",
        external_user_id="ext-user-1",
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
        session_id="session-abc",
    )
    store.get_identity_by_external.return_value = identity

    parsed_msg = CommsMessage(
        id="msg-1",
        channel_id="chan-1",
        direction="inbound",
        content="Hi!",
        identity_id="ext-user-1",
        created_at="2024-01-01T00:00:00",
    )

    mock_adapter = make_adapter()
    mock_adapter.parse_webhook.return_value = [parsed_msg]
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    stored = await manager.handle_inbound("test-channel", {}, {})
    assert stored[0].session_id == "session-abc"
    assert stored[0].identity_id == "identity-1"


@pytest.mark.asyncio
async def test_adapter_rate_limit_callback_wires_to_limiter():
    """Verify that the adapter's rate_limit_callback correctly updates the manager's rate limiter."""
    channel = make_channel(channel_id="chan-rate-limit")
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    # Capture the callback that manager sets on the adapter
    captured_callback = None

    def set_callback(cb):
        nonlocal captured_callback
        captured_callback = cb

    mock_adapter.set_rate_limit_callback.side_effect = set_callback
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    assert captured_callback is not None

    # Manually trigger the callback
    captured_callback(5.0, False)  # 5 seconds backoff

    # Verify backoff is set in the rate limiter
    # TokenBucketRateLimiter.check should return False due to backoff
    assert manager._rate_limiter.check("chan-rate-limit") is False


@pytest.mark.asyncio
async def test_add_channel_creates_and_initializes():
    """add_channel() saves to DB and initializes adapter."""
    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter(channel_type="slack")
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        channel = await manager.add_channel("slack", "my-slack", {"token": "$secret:SLACK_TOKEN"})

    assert channel.name == "my-slack"
    assert channel.channel_type == "slack"
    store.create_channel.assert_called_once()
    assert "my-slack" in manager._adapters


@pytest.mark.asyncio
async def test_add_channel_stores_secrets_in_secret_store():
    """add_channel() stores non-webhook secrets in SecretStore and puts $secret: refs in config."""
    store = make_store()
    secret_store = make_secret_store()
    manager = CommunicationsManager(make_config(), store, secret_store, MagicMock())

    mock_adapter = make_adapter(channel_type="slack")
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    secrets = {
        "bot_token": "xoxb-test-token",
        "signing_secret": "abc123",
        "webhook_secret": "whsec_keep_separate",
    }

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        channel = await manager.add_channel("slack", "my-slack", {}, secrets=secrets)

    # webhook_secret is on the channel config, not in SecretStore
    assert channel.webhook_secret == "whsec_keep_separate"

    # bot_token and signing_secret stored in SecretStore
    assert secret_store.set.call_count == 2
    set_calls = {call.kwargs["name"]: call for call in secret_store.set.call_args_list}
    assert "COMMS_SLACK_BOT_TOKEN_MY-SLACK" in set_calls
    assert "COMMS_SLACK_SIGNING_SECRET_MY-SLACK" in set_calls

    # Config should have $secret: references
    created_channel = store.create_channel.call_args[0][0]
    assert created_channel.config_json["bot_token"] == "$secret:COMMS_SLACK_BOT_TOKEN_MY-SLACK"
    assert (
        created_channel.config_json["signing_secret"]
        == "$secret:COMMS_SLACK_SIGNING_SECRET_MY-SLACK"
    )


@pytest.mark.asyncio
async def test_add_channel_skips_empty_secrets():
    """add_channel() skips empty secret values."""
    store = make_store()
    secret_store = make_secret_store()
    manager = CommunicationsManager(make_config(), store, secret_store, MagicMock())

    mock_adapter = make_adapter(channel_type="slack")
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    secrets = {"bot_token": "xoxb-real", "signing_secret": "", "webhook_secret": ""}

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.add_channel("slack", "my-slack", {}, secrets=secrets)

    # Only bot_token stored (signing_secret and webhook_secret are empty)
    assert secret_store.set.call_count == 1
    assert secret_store.set.call_args.kwargs["name"] == "COMMS_SLACK_BOT_TOKEN_MY-SLACK"


@pytest.mark.asyncio
async def test_remove_channel_shuts_down_and_deletes():
    """remove_channel() shuts down adapter and deletes from DB."""
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    await manager.remove_channel("test-channel")

    mock_adapter.shutdown.assert_called_once()
    store.delete_channel.assert_called_once_with("chan-1")
    assert "test-channel" not in manager._adapters


@pytest.mark.asyncio
async def test_remove_channel_not_found_noop():
    """remove_channel() is a no-op for unknown channel names."""
    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    # Should not raise
    await manager.remove_channel("nonexistent")
    store.delete_channel.assert_not_called()
    assert store.delete_channel.call_count == 0
    assert not store.delete_channel.called


def test_list_channels():
    """list_channels() returns all channels from DB."""
    channels = [make_channel("ch1"), make_channel("ch2", channel_id="chan-2")]
    store = make_store(channels)
    store.list_channels.return_value = channels
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    result = manager.list_channels()
    assert len(result) == 2
    store.list_channels.assert_called_with(enabled_only=False)


def test_get_channel_status_active():
    """get_channel_status() returns active status for running adapter."""
    channel = make_channel()
    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    manager._adapters["test-channel"] = mock_adapter
    manager._channel_by_name["test-channel"] = channel

    status = manager.get_channel_status("test-channel")
    assert status["status"] == "active"
    assert status["active"] is True
    assert status["supports_webhooks"] is True


def test_get_channel_status_inactive():
    """get_channel_status() returns inactive for DB-only channel."""
    channel = make_channel()
    store = make_store()
    store.list_channels.return_value = [channel]
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    status = manager.get_channel_status("test-channel")
    assert status["status"] == "inactive"
    assert status["active"] is False


def test_get_channel_status_not_found():
    """get_channel_status() returns not_found for unknown channel."""
    store = make_store()
    store.list_channels.return_value = []
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    status = manager.get_channel_status("ghost-channel")
    assert status["status"] == "not_found"
    assert status["active"] is False


def test_get_channel_delegates_to_store():
    """get_channel() delegates to store.get_channel()."""
    channel = make_channel()
    store = make_store()
    store.get_channel.return_value = channel
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    result = manager.get_channel("chan-1")
    assert result == channel
    store.get_channel.assert_called_once_with("chan-1")


def test_get_channel_returns_none_for_missing():
    """get_channel() returns None when channel doesn't exist."""
    store = make_store()
    store.get_channel.return_value = None
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    result = manager.get_channel("nonexistent")
    assert result is None


def test_update_channel_delegates_to_store():
    """update_channel() delegates to store and sets updated_at."""
    channel = make_channel()
    store = make_store()
    store.update_channel.return_value = channel
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    result = manager.update_channel(channel)

    assert result == channel
    store.update_channel.assert_called_once_with(channel)
    # updated_at should be refreshed
    assert channel.updated_at != "2024-01-01T00:00:00"


@pytest.mark.asyncio
async def test_send_message_injects_platform_destination():
    """send_message() injects platform_destination from channel config."""
    channel = make_channel(config_json={"default_destination": "C0123ABCD"})
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    msg = await manager.send_message("test-channel", "Hello!")

    assert msg.metadata_json.get("platform_destination") == "C0123ABCD"


@pytest.mark.asyncio
async def test_send_message_preserves_caller_platform_destination():
    """send_message() does not override platform_destination if caller provided it."""
    channel = make_channel(config_json={"default_destination": "C0123ABCD"})
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    msg = await manager.send_message(
        "test-channel", "Hello!", metadata={"platform_destination": "COVERRIDE"}
    )

    assert msg.metadata_json["platform_destination"] == "COVERRIDE"


@pytest.mark.asyncio
async def test_send_message_no_platform_destination_without_config():
    """send_message() does not inject platform_destination when channel has no default."""
    channel = make_channel(config_json={})
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    msg = await manager.send_message("test-channel", "Hello!")

    assert "platform_destination" not in msg.metadata_json


@pytest.mark.asyncio
async def test_send_message_propagates_thread_id():
    """send_message() should include platform_thread_id from thread map."""
    channel = make_channel(webhook_secret=None)
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter.send_message.return_value = "out-msg-1"
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    manager._thread_manager.track_thread("test-channel", "session-123", "thread-456")

    msg = await manager.send_message("test-channel", "Hello reply", session_id="session-123")

    assert msg.platform_thread_id == "thread-456"
    assert msg.status == "sent"


@pytest.mark.asyncio
async def test_handle_inbound_populates_thread_map_and_handles_reactions():
    """handle_inbound_messages() should populate thread map and dispatch reactions."""
    channel = make_channel(webhook_secret=None)
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_identity = CommsIdentity(
        id="id-1",
        channel_id="chan-1",
        external_user_id="user-1",
        session_id="session-123",
        created_at="",
        updated_at="",
    )

    manager._identity_manager = MagicMock()
    manager._identity_manager.resolve_identity = MagicMock(return_value=mock_identity)

    manager.reaction_handler = AsyncMock()

    inbound_msg = CommsMessage(
        id="msg-1",
        channel_id="chan-1",
        direction="inbound",
        content="Hello",
        platform_thread_id="thread-456",
        created_at="",
        identity_id="user-1",
    )

    rxn_msg = CommsMessage(
        id="rxn-1",
        channel_id="chan-1",
        direction="inbound",
        content="+1",
        platform_message_id="msg-123",
        content_type="reaction",
        created_at="",
        identity_id="user-1",
    )

    # Needs to be dict with .get("channel_type") so _channel_by_name works, but manager.start() does that
    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)
    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    await manager.handle_inbound_messages("test-channel", [inbound_msg, rxn_msg])

    assert manager._thread_manager._thread_map[("test-channel", "session-123")] == "thread-456"

    # reaction should have called handler
    manager.reaction_handler.handle_reaction.assert_awaited_once_with(
        "test-channel", "msg-123", "+1", "user-1"
    )
    assert manager.reaction_handler.handle_reaction.await_count == 1
    assert manager.reaction_handler.handle_reaction.await_args is not None


def test_thread_map_lru_eviction_order():
    """Unit test of internal LRU thread map — no public API exposes this behavior."""
    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._thread_manager._max_size = 3

    # Add 3 entries
    manager._track_thread("ch", "s1", "t1")
    manager._track_thread("ch", "s2", "t2")
    manager._track_thread("ch", "s3", "t3")

    # Access s1 to make it recently used
    assert manager._get_thread_id("ch", "s1") == "t1"

    # Add a 4th entry — should evict s2 (LRU), NOT s1 (recently accessed)
    manager._track_thread("ch", "s4", "t4")

    assert manager._get_thread_id("ch", "s1") == "t1"  # Still present (was accessed)
    assert manager._get_thread_id("ch", "s2") is None  # Evicted (LRU)
    assert manager._get_thread_id("ch", "s3") == "t3"  # Still present
    assert manager._get_thread_id("ch", "s4") == "t4"  # Newly added


def test_thread_map_move_to_end_on_track():
    """Unit test of internal LRU refresh — no public API exposes this behavior."""
    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._thread_manager._max_size = 2

    manager._track_thread("ch", "s1", "t1")
    manager._track_thread("ch", "s2", "t2")

    # Re-track s1 (refreshes its position)
    manager._track_thread("ch", "s1", "t1-updated")

    # Add s3 — should evict s2 (now LRU), not s1
    manager._track_thread("ch", "s3", "t3")

    assert manager._get_thread_id("ch", "s1") == "t1-updated"
    assert manager._get_thread_id("ch", "s2") is None  # Evicted
    assert manager._get_thread_id("ch", "s3") == "t3"


def test_routing_rule_crud_invalidates_cache():
    """Manager routing rule CRUD methods should invalidate router cache."""
    from gobby.communications.models import CommsRoutingRule

    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    rule = CommsRoutingRule(
        id="rule-1",
        name="Test Rule",
        channel_id="chan-1",
        event_pattern="task.*",
        priority=10,
    )

    # Populate cache by setting it directly
    manager._router._rules_cache = [rule]
    manager._router._cache_expires_at = float("inf")

    # Create should invalidate
    store.create_routing_rule.return_value = rule
    manager.create_routing_rule(rule)
    assert manager._router._rules_cache is None

    # Repopulate cache
    manager._router._rules_cache = [rule]
    manager._router._cache_expires_at = float("inf")

    # Update should invalidate
    store.update_routing_rule.return_value = rule
    manager.update_routing_rule(rule)
    assert manager._router._rules_cache is None

    # Repopulate cache
    manager._router._rules_cache = [rule]
    manager._router._cache_expires_at = float("inf")

    # Delete should invalidate
    manager.delete_routing_rule("rule-1")
    assert manager._router._rules_cache is None
