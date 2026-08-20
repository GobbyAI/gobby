"""Tests for CommunicationsManager."""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gobby.communications.adapters.slack import SlackAdapter
from gobby.communications.adapters.sms import SMSAdapter
from gobby.communications.adapters.teams import TeamsAdapter
from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.identities import IdentityResolution
from gobby.communications.manager import CommunicationsManager
from gobby.communications.models import (
    ChannelConfig,
    CommsAttachment,
    CommsIdentity,
    CommsMessage,
    CommsRoutingRule,
)
from gobby.communications.rate_limiter import RateLimitWaitExceeded
from gobby.communications.voice import apply_voice_transcription
from gobby.config.communications import ChannelDefaults, CommunicationsConfig
from gobby.storage.communications import LocalCommunicationsStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore
from gobby.utils.datetime import datetime_to_local_iso


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
    stored_channels = channels or []
    store.list_channels.return_value = stored_channels
    store.get_channel_by_name.side_effect = lambda name: next(
        (channel for channel in store.list_channels.return_value if channel.name == name),
        None,
    )

    def update_channel(updated: ChannelConfig) -> ChannelConfig:
        store.list_channels.return_value = [
            updated if channel.id == updated.id else channel
            for channel in store.list_channels.return_value
        ]
        return updated

    store.update_channel.side_effect = update_channel
    store.get_routing_rules.return_value = []
    store.create_message.side_effect = lambda message: message

    def create_message_with_attachments(
        message: CommsMessage,
        attachments: list[CommsAttachment],
    ) -> tuple[CommsMessage, list[CommsAttachment]]:
        for attachment in attachments:
            attachment.message_id = message.id
        return message, attachments

    store.create_message_with_attachments.side_effect = create_message_with_attachments
    store.create_channel.return_value = None
    store.get_message_by_platform_id.return_value = None

    def delete_channel(channel_id: str) -> None:
        store.list_channels.return_value = [
            channel for channel in store.list_channels.return_value if channel.id != channel_id
        ]

    store.delete_channel.side_effect = delete_channel
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
    adapter.send_proactive = AsyncMock(return_value="platform-proactive-id-1")
    adapter.shutdown = AsyncMock()
    adapter.parse_webhook.return_value = []
    adapter.download_inbound_attachments = AsyncMock(return_value=[])
    adapter.acknowledge_webhook_messages = AsyncMock()
    adapter.verify_webhook.return_value = True
    return adapter


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_loads_channels() -> None:
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
    # start() lists channels for gobby_chat creation, then enabled channels for activation.
    assert store.list_channels.call_count == 2
    store.list_channels.assert_any_call(enabled_only=False)
    store.list_channels.assert_any_call(enabled_only=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_polls_poll_only_adapter_with_global_webhook_url() -> None:
    """Poll-only adapters keep polling when webhook-capable channels use webhooks."""
    channel = make_channel()
    store = make_store([channel])
    config = make_config()
    config.webhook_base_url = "https://example.com"
    manager = CommunicationsManager(config, store, make_secret_store(), MagicMock())
    manager._polling_manager.start_polling = MagicMock()

    adapter = make_adapter(supports_webhooks=False, supports_polling=True)
    adapter_cls = MagicMock(return_value=adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=adapter_cls):
        await manager.start()

    assert manager._adapters[channel.name] is adapter
    assert manager._channel_by_name[channel.name] is channel
    manager._polling_manager.start_polling.assert_called_once_with(channel.name, adapter, None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adapter_config_updates_persist_through_channel_store() -> None:
    channel = make_channel(
        channel_type="telegram",
        config_json={"bot_token": "$secret:telegram-token"},
    )
    store = make_store([channel])
    store.get_channel.return_value = channel
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    adapter = make_adapter(channel_type="telegram")
    adapter_cls = MagicMock(return_value=adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=adapter_cls):
        await manager._init_adapter(channel)

    persist_config = adapter.set_config_update_callback.call_args.args[0]
    await persist_config({"poll_offset": 501})

    assert channel.config_json == {
        "bot_token": "$secret:telegram-token",
        "poll_offset": 501,
    }
    store.update_channel.assert_called_once_with(channel)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_adapter_config_callback_cannot_overwrite_replacement() -> None:
    channel = make_channel(
        channel_type="telegram",
        config_json={"bot_token": "$secret:telegram-token"},
    )
    store = make_store([channel])
    store.get_channel.return_value = channel
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    first_adapter = make_adapter(channel_type="telegram")
    second_adapter = make_adapter(channel_type="telegram")
    adapter_cls = MagicMock(side_effect=[first_adapter, second_adapter])

    with patch("gobby.communications.manager.get_adapter_class", return_value=adapter_cls):
        await manager._init_adapter(channel)
        stale_callback = first_adapter.set_config_update_callback.call_args.args[0]
        manager._adapters[channel.name] = first_adapter
        manager._channel_by_name[channel.name] = channel
        await manager.update_channel(channel)

    store.update_channel.reset_mock()
    await stale_callback({"poll_offset": 100})

    store.update_channel.assert_not_called()
    assert channel.config_json == {"bot_token": "$secret:telegram-token"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_telegram_group_message_skips_telegram_group_policy() -> None:
    channel = make_channel(channel_type="slack")
    manager = CommunicationsManager(
        make_config(),
        make_store([channel]),
        make_secret_store(),
        MagicMock(),
    )
    message = CommsMessage(
        id="message-1",
        channel_id=channel.id,
        direction="inbound",
        content="hello group",
        metadata_json={"is_group": True},
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )

    with patch(
        "gobby.communications.manager.evaluate_group_message",
        side_effect=AssertionError("telegram-only policy was called"),
    ):
        admitted = await manager.admit_inbound_message(channel, message)

    assert admitted is True


@pytest.mark.unit
async def test_telegram_init_uses_global_webhook_url_as_inbound_source() -> None:
    """Telegram webhook setup follows the manager's global inbound mode decision."""
    channel = make_channel(channel_type="telegram", config_json={})
    store = make_store([channel])
    config = make_config()
    config.webhook_base_url = "https://global.example/hooks"
    manager = CommunicationsManager(config, store, make_secret_store(), MagicMock())
    manager._polling_manager.start_polling = MagicMock()

    adapter = make_adapter(channel_type="telegram", supports_webhooks=True, supports_polling=True)

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=adapter),
    ):
        await manager.start()

    init_channel = adapter.initialize.call_args.args[0]
    assert manager._adapters[channel.name] is adapter
    assert "webhook_base_url" not in channel.config_json
    assert init_channel.config_json["webhook_base_url"] == "https://global.example/hooks"
    manager._polling_manager.start_polling.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_telegram_init_resolves_webhook_secret_reference() -> None:
    channel = make_channel(
        channel_type="telegram",
        config_json={},
        webhook_secret="$secret:COMMS_TELEGRAM_WEBHOOK_SECRET_MY_TELEGRAM",
    )
    store = make_store([channel])
    secret_store = make_secret_store()
    secret_store.get.return_value = "resolved-webhook-secret"
    manager = CommunicationsManager(make_config(), store, secret_store, MagicMock())
    adapter = make_adapter(channel_type="telegram", supports_webhooks=True, supports_polling=True)

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=adapter),
    ):
        await manager.start()

    init_channel = adapter.initialize.call_args.args[0]
    assert channel.webhook_secret == "$secret:COMMS_TELEGRAM_WEBHOOK_SECRET_MY_TELEGRAM"
    assert init_channel.webhook_secret == "resolved-webhook-secret"
    secret_store.get.assert_called_once_with("COMMS_TELEGRAM_WEBHOOK_SECRET_MY_TELEGRAM")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_telegram_init_removes_stale_channel_webhook_url_when_polling() -> None:
    """Telegram polling setup cannot leave a channel-level webhook registered."""
    channel = make_channel(
        channel_type="telegram",
        config_json={"webhook_base_url": "https://stale.example/hooks"},
    )
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._polling_manager.start_polling = MagicMock()

    adapter = make_adapter(channel_type="telegram", supports_webhooks=True, supports_polling=True)

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=adapter),
    ):
        await manager.start()

    init_channel = adapter.initialize.call_args.args[0]
    assert manager._adapters[channel.name] is adapter
    assert channel.config_json["webhook_base_url"] == "https://stale.example/hooks"
    assert "webhook_base_url" not in init_channel.config_json
    manager._polling_manager.start_polling.assert_called_once_with(channel.name, adapter, None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_skips_unknown_adapter() -> None:
    """start() logs error but continues if adapter type is unknown."""
    channel = make_channel(channel_type="unknown_type")
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    with patch("gobby.communications.manager.get_adapter_class", return_value=None):
        await manager.start()

    assert "test-channel" not in manager._adapters


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_rejects_invalid_channel_rate_limit_before_activation() -> None:
    channel = make_channel(config_json={"rate_limit_per_minute": 0, "burst": 1})
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter_cls = MagicMock(return_value=make_adapter())

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    assert "test-channel" not in manager._adapters
    mock_adapter_cls.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_shuts_down_all_adapters() -> None:
    """stop() calls shutdown on all active adapters and clears state."""
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    events: list[str] = []

    mock_adapter = make_adapter()
    mock_adapter.shutdown.side_effect = lambda: events.append("adapter")
    mock_adapter_cls = MagicMock(return_value=mock_adapter)
    vision_service = MagicMock()
    vision_service.stop = AsyncMock(side_effect=lambda: events.append("vision"))
    manager.set_vision_extract_service(vision_service)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    manager.responder.stop = AsyncMock(side_effect=lambda: events.append("responder"))
    await manager.stop()

    mock_adapter.shutdown.assert_called_once()
    vision_service.stop.assert_awaited_once_with()
    assert events == ["responder", "adapter", "vision"]
    assert len(manager._adapters) == 0
    assert len(manager._channel_by_name) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_success() -> None:
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_set_reaction_delegates_to_capable_adapter() -> None:
    channel = make_channel(channel_type="telegram")
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    mock_adapter = make_adapter(channel_type="telegram")
    mock_adapter.supports_reactions = True
    mock_adapter.set_reaction = AsyncMock()

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()

    assert manager.supports_reactions("test-channel") is True
    await manager.set_reaction("test-channel", "chat-1", "message-1", "👀")

    mock_adapter.set_reaction.assert_awaited_once_with(
        "chat-1",
        "message-1",
        "👀",
    )
    manager._adapters.clear()
    assert manager.supports_reactions("test-channel") is False
    with pytest.raises(ValueError, match="not found or not active"):
        await manager.set_reaction("test-channel", "chat-1", "message-1", None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_edit_message_persists_final_content(temp_db: HubDatabase) -> None:
    channel = make_channel(channel_id="00000000-0000-0000-0000-000000000101")
    store = LocalCommunicationsStore(
        temp_db,
        project_id="00000000-0000-0000-0000-000000000000",
    )
    store.create_channel(channel)
    stored_message = CommsMessage(
        id="00000000-0000-0000-0000-000000000102",
        channel_id=channel.id,
        direction="outbound",
        content="Thinking…",
        platform_message_id="platform-msg-id-1",
        created_at=datetime.now(UTC),
    )
    store.create_message(stored_message)
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter.supports_message_edit = True
    mock_adapter.edit_message = AsyncMock()
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()

    await manager.edit_message(
        "test-channel",
        "platform-msg-id-1",
        "Final response",
        "conversation-1",
    )

    mock_adapter.edit_message.assert_awaited_once_with(
        "platform-msg-id-1",
        "Final response",
        "conversation-1",
    )
    persisted = store.get_message("00000000-0000-0000-0000-000000000102")
    assert persisted is not None
    assert persisted.content == "Final response"
    assert [message.id for message in store.list_messages(channel_id=channel.id)] == [
        "00000000-0000-0000-0000-000000000102"
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_unknown_channel_raises() -> None:
    """send_message() raises ValueError for unknown channel."""
    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    with pytest.raises(ValueError, match="not found or not active"):
        await manager.send_message("no-such-channel", "Hello!")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_adapter_failure_marks_failed() -> None:
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_rate_limit_timeout_marks_failed() -> None:
    """send_message() marks message failed if rate-limit waiting exceeds its bound."""
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    with patch.object(
        manager._rate_limiter,
        "wait_if_needed",
        AsyncMock(side_effect=RateLimitWaitExceeded("rate limit wait exceeded")),
    ):
        msg = await manager.send_message("test-channel", "Hello!")

    assert msg.status == "failed"
    assert "rate limit wait exceeded" in (msg.error or "")
    mock_adapter.send_message.assert_not_called()
    store.create_message.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_proactive_rate_limits_and_persists_message() -> None:
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    with patch.object(manager._rate_limiter, "wait_if_needed", AsyncMock()) as wait_mock:
        msg = await manager.send_proactive("test-channel", "conversation-1", "Hello!")

    wait_mock.assert_awaited_once_with("chan-1")
    mock_adapter.send_proactive.assert_awaited_once_with("conversation-1", "Hello!", "text")
    assert msg.status == "sent"
    assert msg.platform_message_id == "platform-proactive-id-1"
    assert msg.metadata_json["platform_destination"] == "conversation-1"
    store.create_message.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_gobby_chat_without_broadcast_marks_failed() -> None:
    channel = make_channel(
        name="gobby_chat",
        channel_type="gobby_chat",
        channel_id="gobby-chat-1",
    )
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    await manager.start()

    msg = await manager.send_message("gobby_chat", "Hello!")

    assert msg.status == "failed"
    assert "broadcast callable is not configured" in (msg.error or "")
    store.create_message.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_set_websocket_broadcast_before_start_wires_gobby_chat_on_start() -> None:
    channel = make_channel(
        name="gobby_chat",
        channel_type="gobby_chat",
        channel_id="gobby-chat-1",
    )
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    broadcast = AsyncMock()

    manager.set_websocket_broadcast(broadcast)
    await manager.start()

    msg = await manager.send_message("gobby_chat", "Hello!")

    assert msg.status == "sent"
    assert msg.platform_message_id is not None
    broadcast.assert_awaited_once()
    payload = broadcast.await_args.args[0]
    assert payload["content"] == "Hello!"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_telegram_4xx_redacts_token_from_logs_and_storage(caplog) -> None:
    token = "test-telegram-token"
    channel = make_channel(
        channel_type="telegram",
        config_json={
            "bot_token": "$secret:TELEGRAM_BOT_TOKEN",
            "default_destination": "chat999",
        },
    )
    store = make_store([channel])
    secret_store = make_secret_store()
    secret_store.get.return_value = token
    manager = CommunicationsManager(make_config(), store, secret_store, MagicMock())

    mock_post = AsyncMock()

    async def side_effect(url, **kwargs):
        request = httpx.Request("POST", url)
        if "deleteWebhook" in url:
            return httpx.Response(200, request=request, json={"ok": True})
        if "getMe" in url:
            return httpx.Response(
                200,
                request=request,
                json={"ok": True, "result": {"id": 123456, "username": "gobby_bot"}},
            )
        if "setMyCommands" in url:
            return httpx.Response(200, request=request, json={"ok": True, "result": True})
        return httpx.Response(400, request=request, json={"ok": False})

    mock_post.side_effect = side_effect

    with (
        patch("httpx.AsyncClient") as MockClient,
        patch("gobby.communications.manager.get_adapter_class", return_value=TelegramAdapter),
    ):
        MockClient.return_value.post = mock_post
        await manager.start()

        with caplog.at_level(logging.ERROR, logger="gobby.communications.manager"):
            msg = await manager.send_message("test-channel", "Hello!")

    stored_message = store.create_message.call_args.args[0]
    assert msg.status == "failed"
    assert msg.error == stored_message.error
    assert stored_message.error is not None
    assert "400" in stored_message.error
    assert "***" in stored_message.error
    assert token not in stored_message.error
    assert token not in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_fires_event_callback() -> None:
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_logs_event_callback_failures_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    async def cb(event_type: str, **kwargs: Any) -> None:
        raise RuntimeError("callback broke")

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    manager.event_callback = cb
    with caplog.at_level(logging.WARNING):
        await manager.send_message("test-channel", "Hello!")

    assert "Event callback error on send_message" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_event_routes_to_channels() -> None:
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_send_event_id_is_durable_across_manager_restart(temp_db: HubDatabase) -> None:
    """A recovered event returns its persisted message without sending twice."""
    channel = make_channel(
        channel_type="telegram",
        channel_id="10000000-0000-0000-0000-000000000001",
        config_json={"default_destination": "chat-123"},
    )
    project_id = "project-1"
    now = datetime.now(UTC)
    store = LocalCommunicationsStore(temp_db)
    store.create_channel(channel)
    store.create_routing_rule(
        CommsRoutingRule(
            id="20000000-0000-0000-0000-000000000001",
            name="Cron home notifications",
            channel_id=channel.id,
            event_pattern="cron.run.*",
            created_at=now,
            updated_at=now,
        )
    )

    first_adapter = make_adapter(channel_type="telegram")
    first_manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=first_adapter),
    ):
        await first_manager.start()

    first = await first_manager.send_event(
        "cron.run.completed",
        'Scheduled job "Nightly backup" completed.',
        project_id=project_id,
        event_id="cron-run-1",
    )

    second_adapter = make_adapter(channel_type="telegram")
    recovered_manager = CommunicationsManager(
        make_config(), store, make_secret_store(), MagicMock()
    )
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=second_adapter),
    ):
        await recovered_manager.start()

    recovered = await recovered_manager.send_event(
        "cron.run.completed",
        'Scheduled job "Nightly backup" completed.',
        project_id=project_id,
        event_id="cron-run-1",
    )

    first_adapter.send_message.assert_awaited_once()
    second_adapter.send_message.assert_not_awaited()
    assert len(first) == len(recovered) == 1
    assert recovered[0].id == first[0].id

    persisted = store.get_message(first[0].id)
    assert persisted is not None
    assert persisted.status == "sent"
    assert persisted.platform_message_id == "platform-msg-id-1"
    assert persisted.metadata_json == {
        "platform_destination": "chat-123",
        "source_event_id": "cron-run-1",
    }


@pytest.mark.unit
async def test_send_event_skips_inactive_channels() -> None:
    """send_event() skips channel IDs that don't have active adapters."""
    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    manager._router.match_channels = AsyncMock(return_value=["chan-inactive"])

    msgs = await manager.send_event("task.created", "Hello!")
    assert msgs == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_stores_messages() -> None:
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

    stored = await manager.handle_inbound(
        "test-channel", {"data": "payload"}, {}, raw_body=b'{"data":"payload"}'
    )

    assert len(stored) == 1
    assert stored[0].content == "Hi there!"
    store.create_message.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_persists_downloaded_attachments() -> None:
    channel = make_channel(webhook_secret=None)
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    parsed_msg = CommsMessage(
        id="msg-1",
        channel_id="",
        direction="inbound",
        content="document caption",
        content_type="attachment",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    attachment = CommsAttachment(
        id="attachment-1",
        message_id="",
        filename="report.pdf",
        content_type="application/pdf",
        size_bytes=17,
        local_path="/tmp/report.pdf",
    )
    mock_adapter = make_adapter()
    mock_adapter.parse_webhook.return_value = [parsed_msg]
    mock_adapter.download_inbound_attachments.return_value = [attachment]

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()

    stored = await manager.handle_inbound(
        "test-channel",
        {"data": "payload"},
        {},
        raw_body=b'{"data":"payload"}',
    )

    assert len(stored) == 1
    mock_adapter.download_inbound_attachments.assert_awaited_once_with(
        stored[0],
        manager.attachment_manager,
    )
    assert attachment.message_id == stored[0].id
    store.create_message_with_attachments.assert_called_once_with(stored[0], [attachment])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_transcribes_voice_note_before_event(tmp_path: Path) -> None:
    channel = make_channel(webhook_secret=None)
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"telegram voice bytes")
    parsed_msg = CommsMessage(
        id="voice-message",
        channel_id="",
        direction="inbound",
        content="original caption",
        content_type="attachment",
        metadata_json={"voice_note": True},
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    attachment = CommsAttachment(
        id="voice-attachment",
        message_id="",
        filename="voice.ogg",
        content_type="audio/ogg",
        size_bytes=20,
        local_path=str(audio_path),
    )
    transcriber = MagicMock()
    transcriber.transcribe = AsyncMock(return_value="  transcribed voice note  ")
    manager.set_voice_transcriber_getter(lambda: transcriber)
    manager.event_callback = AsyncMock()
    mock_adapter = make_adapter()
    mock_adapter.parse_webhook.return_value = [parsed_msg]
    mock_adapter.download_inbound_attachments.return_value = [attachment]

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()

    stored = await manager.handle_inbound(
        "test-channel",
        {"data": "payload"},
        {},
        raw_body=b'{"data":"payload"}',
    )

    assert stored[0].content == "transcribed voice note"
    assert stored[0].metadata_json["voice_note_caption"] == "original caption"
    assert stored[0].metadata_json["voice_transcription_status"] == "completed"
    assert attachment.message_id == stored[0].id
    transcriber.transcribe.assert_awaited_once_with(b"telegram voice bytes", "audio/ogg")
    store.create_message_with_attachments.assert_called_once_with(stored[0], [attachment])
    manager.event_callback.assert_awaited_once_with(
        "comms.message_received",
        message=stored[0],
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_voice_transcription_timeout_preserves_message_and_marks_failed(
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"voice bytes")
    message = CommsMessage(
        id="voice-message",
        channel_id="channel-1",
        direction="inbound",
        content="original caption",
        content_type="attachment",
        metadata_json={"voice_note": True},
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    attachment = CommsAttachment(
        id="voice-attachment",
        message_id="voice-message",
        filename="voice.ogg",
        content_type="audio/ogg",
        size_bytes=11,
        local_path=str(audio_path),
    )
    transcriber = MagicMock()

    async def slow_transcribe(*_args: object) -> str:
        await asyncio.Event().wait()
        return "late transcript"

    transcriber.transcribe = AsyncMock(side_effect=slow_transcribe)

    await apply_voice_transcription(
        message,
        [attachment],
        transcriber,
        timeout_seconds=0.001,
    )

    assert message.content == "original caption"
    assert message.metadata_json["voice_transcription_status"] == "failed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_describes_sticker_before_event(tmp_path: Path) -> None:
    channel = make_channel(webhook_secret=None)
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    image_path = tmp_path / "sticker.webp"
    image_path.write_bytes(b"telegram sticker image")
    parsed_msg = CommsMessage(
        id="sticker-message",
        channel_id="",
        direction="inbound",
        content="",
        content_type="attachment",
        metadata_json={
            "telegram_sticker": {
                "format": "static",
                "emoji": "🦡",
                "set_name": "quartz_badger",
                "type": "regular",
            }
        },
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    attachment = CommsAttachment(
        id="sticker-attachment",
        message_id="",
        filename="sticker.webp",
        content_type="image/webp",
        size_bytes=22,
        local_path=str(image_path),
    )
    vision_result = MagicMock(
        text="A badger holding a sparkling quartz crystal.",
        provider="claude",
        model="sonnet",
    )
    vision_service = MagicMock()
    vision_service.extract = AsyncMock(return_value=vision_result)
    manager.set_vision_extract_service(vision_service)
    manager.event_callback = AsyncMock()
    mock_adapter = make_adapter()
    mock_adapter.parse_webhook.return_value = [parsed_msg]
    mock_adapter.download_inbound_attachments.return_value = [attachment]

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()

    stored = await manager.handle_inbound(
        "test-channel",
        {"data": "payload"},
        {},
        raw_body=b'{"data":"payload"}',
    )

    assert stored[0].content == (
        "Telegram sticker 🦡: A badger holding a sparkling quartz crystal."
    )
    assert stored[0].metadata_json["sticker_vision_status"] == "completed"
    request = vision_service.extract.await_args.args[0]
    assert request.image_path == str(image_path)
    assert attachment.message_id == stored[0].id
    store.create_message_with_attachments.assert_called_once_with(stored[0], [attachment])
    manager.event_callback.assert_awaited_once_with(
        "comms.message_received",
        message=stored[0],
    )


@pytest.mark.unit
async def test_handle_inbound_webhook_verification_failure() -> None:
    """handle_inbound() raises ValueError if webhook signature fails."""
    channel = make_channel(webhook_secret="mysecret")
    store = make_store([channel])
    secret_store = make_secret_store()
    secret_store.get.return_value = "mysecret"
    manager = CommunicationsManager(make_config(), store, secret_store, MagicMock())

    mock_adapter = make_adapter()
    mock_adapter.verify_webhook.return_value = False
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    with pytest.raises(ValueError, match="signature verification failed"):
        await manager.handle_inbound("test-channel", b"payload", {"X-Signature": "bad"})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_verifies_adapter_when_webhook_secret_unset() -> None:
    """Adapters still reject forged webhooks when channel.webhook_secret is unset."""
    manager = CommunicationsManager(make_config(), make_store([]), make_secret_store(), MagicMock())
    slack_adapter = SlackAdapter()
    sms_adapter = SMSAdapter()
    teams_adapter = TeamsAdapter()
    slack_adapter._signing_secret = "slack-signing-secret"
    sms_adapter._auth_token = "twilio-auth-token"
    sms_adapter._webhook_url = "https://example.com/hooks/sms"
    teams_adapter._app_id = "teams-app-id"
    cases = [
        ("slack-channel", make_channel("slack-channel", "slack"), slack_adapter, {}),
        (
            "sms-channel",
            make_channel("sms-channel", "sms"),
            sms_adapter,
            {"x-twilio-signature": "bad"},
        ),
        ("teams-channel", make_channel("teams-channel", "teams"), teams_adapter, {}),
    ]

    for channel_name, channel, adapter, headers in cases:
        manager._adapters[channel_name] = adapter
        manager._channel_by_name[channel_name] = channel

        with pytest.raises(ValueError, match="signature verification failed"):
            await manager.handle_inbound(channel_name, b"forged", headers)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_resolves_webhook_secret_ref() -> None:
    """handle_inbound() resolves webhook_secret refs before signature verification."""
    channel = make_channel(webhook_secret="$secret:COMMS_SLACK_WEBHOOK_SECRET_MY_SLACK")
    store = make_store([channel])
    secret_store = make_secret_store()
    secret_store.get.return_value = "mysecret"
    manager = CommunicationsManager(make_config(), store, secret_store, MagicMock())

    mock_adapter = make_adapter()
    mock_adapter.verify_webhook.side_effect = (
        lambda _payload, _headers, secret: secret == "mysecret"
    )
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    secret_store.get.reset_mock()
    messages = await manager.handle_inbound("test-channel", b"payload", {"X-Signature": "ok"})

    assert messages == []
    secret_store.get.assert_called_once_with("COMMS_SLACK_WEBHOOK_SECRET_MY_SLACK")
    mock_adapter.verify_webhook.assert_called_once_with(
        b"payload",
        {"X-Signature": "ok"},
        "mysecret",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_resolves_identity() -> None:
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

    stored = await manager.handle_inbound("test-channel", {}, {}, raw_body=b"{}")
    assert stored[0].session_id == "session-abc"
    assert stored[0].identity_id == "identity-1"
    assert stored[0].metadata_json["external_user_id"] == "ext-user-1"


@pytest.mark.unit
async def test_handle_inbound_messages_continues_after_identity_resolution_failure() -> None:
    """A bad inbound message should not abort the rest of the batch."""
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._channel_by_name[channel.name] = channel

    identity = CommsIdentity(
        id="identity-2",
        channel_id="chan-1",
        external_user_id="ext-user-2",
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
        session_id="session-ok",
    )
    object.__setattr__(
        manager._identity_manager,
        "resolve_inbound_identity",
        MagicMock(
            side_effect=[
                RuntimeError("database unavailable"),
                IdentityResolution(identity=identity, session_id="session-ok"),
            ]
        ),
    )

    messages = [
        CommsMessage(
            id="bad-msg",
            channel_id="chan-1",
            direction="inbound",
            content="bad",
            identity_id="ext-user-1",
            created_at="2024-01-01T00:00:00",
        ),
        CommsMessage(
            id="good-msg",
            channel_id="chan-1",
            direction="inbound",
            content="good",
            identity_id="ext-user-2",
            created_at="2024-01-01T00:00:00",
        ),
    ]

    stored = await manager.handle_inbound_messages("test-channel", messages)

    assert [message.content for message in stored] == ["good"]
    assert stored[0].session_id == "session-ok"
    store.create_message.assert_called_once_with(messages[1])


@pytest.mark.parametrize(
    ("adapter_name", "raw_channel_id", "metadata_json", "expected_platform_channel_id"),
    [
        ("slack", "C123", {}, "C123"),
        ("sms", "+15551234567", {}, "+15551234567"),
        ("teams", "conv-123", {}, "conv-123"),
        ("discord", "discord-channel-123", {}, "discord-channel-123"),
        ("email", "sender@example.com", {}, "sender@example.com"),
        ("telegram", "", {"chat_id": "2222222"}, "2222222"),
        ("gobby_chat", "gobby_chat", {}, "gobby_chat"),
    ],
)
@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_messages_stores_internal_channel_id(
    adapter_name: str,
    raw_channel_id: str,
    metadata_json: dict[str, str],
    expected_platform_channel_id: str,
) -> None:
    """Inbound messages store the internal channel UUID and preserve platform channel."""
    channel = make_channel(channel_id="internal-channel-id")
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter(channel_type=adapter_name)
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    message = CommsMessage(
        id=f"{adapter_name}-msg-1",
        channel_id=raw_channel_id,
        direction="inbound",
        content="Hi",
        metadata_json=metadata_json,
        created_at="2024-01-01T00:00:00",
    )

    stored = await manager.handle_inbound_messages("test-channel", [message])

    assert stored[0].channel_id == "internal-channel-id"
    assert stored[0].metadata_json["platform_channel_id"] == expected_platform_channel_id
    store.create_message.assert_called_once_with(stored[0])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adapter_rate_limit_callback_wires_to_limiter() -> None:
    """Verify adapter rate_limit_callback updates the manager's rate limiter."""
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_channel_creates_and_initializes() -> None:
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
    assert manager.channel_to_dict(channel)["active"] is True
    assert manager.channel_to_dict(channel)["init_error"] is None


@pytest.mark.unit
async def test_add_channel_returns_inactive_with_init_error_on_adapter_failure() -> None:
    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter(channel_type="slack")
    mock_adapter.initialize.side_effect = RuntimeError("bad token")
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        channel = await manager.add_channel("slack", "my-slack", {"token": "$secret:SLACK_TOKEN"})

    payload = manager.channel_to_dict(channel)
    assert payload["active"] is False
    assert payload["init_error"] == "bad token"
    assert "my-slack" not in manager._adapters
    store.create_channel.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_channel_stores_secrets_in_secret_store() -> None:
    """add_channel() stores secrets in SecretStore and puts refs in channel config."""
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

    assert channel.webhook_secret == "$secret:COMMS_SLACK_WEBHOOK_SECRET_MY_SLACK"

    # bot_token, signing_secret, and webhook_secret stored in SecretStore
    assert secret_store.set.call_count == 3
    set_calls = {call.kwargs["name"]: call for call in secret_store.set.call_args_list}
    assert "COMMS_SLACK_BOT_TOKEN_MY_SLACK" in set_calls
    assert "COMMS_SLACK_SIGNING_SECRET_MY_SLACK" in set_calls
    assert "COMMS_SLACK_WEBHOOK_SECRET_MY_SLACK" in set_calls

    # Config should have $secret: references
    created_channel = store.create_channel.call_args[0][0]
    assert created_channel.config_json["bot_token"] == "$secret:COMMS_SLACK_BOT_TOKEN_MY_SLACK"
    assert (
        created_channel.config_json["signing_secret"]
        == "$secret:COMMS_SLACK_SIGNING_SECRET_MY_SLACK"
    )
    assert "webhook_secret" not in created_channel.config_json
    assert created_channel.webhook_secret == "$secret:COMMS_SLACK_WEBHOOK_SECRET_MY_SLACK"
    assert secret_store.set.call_args_list[-1].kwargs["plaintext_value"] == "whsec_keep_separate"


@pytest.mark.integration
async def test_add_channel_persists_webhook_secret_reference(temp_db, mock_machine_id: str) -> None:
    """New channel rows contain a SecretStore reference instead of plaintext."""
    assert mock_machine_id
    store = LocalCommunicationsStore(temp_db)
    secret_store = SecretStore(temp_db)
    manager = CommunicationsManager(make_config(), store, secret_store, MagicMock())
    mock_adapter = make_adapter(channel_type="slack")

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        channel = await manager.add_channel(
            "slack",
            "db-backed-slack",
            {},
            secrets={"webhook_secret": "plaintext-webhook-secret"},
        )

    stored = store.get_channel(channel.id)
    assert stored is not None
    assert stored.webhook_secret == "$secret:COMMS_SLACK_WEBHOOK_SECRET_DB_BACKED_SLACK"
    assert (
        secret_store.get("COMMS_SLACK_WEBHOOK_SECRET_DB_BACKED_SLACK") == "plaintext-webhook-secret"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_channel_does_not_mutate_caller_config() -> None:
    store = make_store()
    secret_store = make_secret_store()
    manager = CommunicationsManager(make_config(), store, secret_store, MagicMock())

    mock_adapter = make_adapter(channel_type="slack")
    mock_adapter_cls = MagicMock(return_value=mock_adapter)
    config = {"token": "$secret:SLACK_TOKEN"}
    secrets = {"bot_token": "xoxb-test-token"}

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.add_channel("slack", "my-slack", config, secrets=secrets)

    assert config == {"token": "$secret:SLACK_TOKEN"}
    created_channel = store.create_channel.call_args[0][0]
    assert created_channel.config_json["bot_token"] == "$secret:COMMS_SLACK_BOT_TOKEN_MY_SLACK"


@pytest.mark.unit
def test_channel_to_dict_redacts_webhook_secret() -> None:
    channel = make_channel(webhook_secret="$secret:COMMS_SLACK_WEBHOOK_SECRET_MY_SLACK")
    manager = CommunicationsManager(
        make_config(), make_store([channel]), make_secret_store(), MagicMock()
    )

    payload = manager.channel_to_dict(channel)

    assert "webhook_secret" not in payload
    assert payload["active"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_init_adapter_offloads_secret_ref_store_reads() -> None:
    loop_thread_id = threading.get_ident()
    secret_store = make_secret_store()

    def get_secret(name: str) -> str | None:
        assert threading.get_ident() != loop_thread_id
        return "resolved-token" if name == "COMMS_TEST_TOKEN" else None

    secret_store.get.side_effect = get_secret
    manager = CommunicationsManager(make_config(), make_store(), secret_store, MagicMock())
    channel = make_channel(config_json={"bot_token": "$secret:COMMS_TEST_TOKEN"})
    adapter = make_adapter()

    async def initialize(
        _config: ChannelConfig, secret_resolver: Callable[[str], str | None]
    ) -> None:
        assert secret_resolver("$secret:COMMS_TEST_TOKEN") == "resolved-token"
        assert secret_resolver("COMMS_TEST_TOKEN") == "resolved-token"

    adapter.initialize.side_effect = initialize
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=adapter),
    ):
        await manager._init_adapter(channel)

    secret_store.get.assert_any_call("COMMS_TEST_TOKEN")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_init_adapter_resolves_secret_refs_with_real_secret_store(
    temp_db, mock_machine_id: str
) -> None:
    """_init_adapter() passes a ref-aware resolver backed by SecretStore.get."""
    assert mock_machine_id
    secret_store = SecretStore(temp_db)
    secret_store.set(
        name="COMMS_SLACK_BOT_TOKEN_MY_SLACK",
        plaintext_value="xoxb-scoped-token",
        category="integration",
    )
    secret_store.set(
        name="COMMS_SLACK_SIGNING_SECRET_MY_SLACK",
        plaintext_value="scoped-signing-secret",
        category="integration",
    )
    manager = CommunicationsManager(make_config(), make_store(), secret_store, MagicMock())
    channel = ChannelConfig(
        id="secret-backed-slack",
        channel_type="slack",
        name="my-slack",
        enabled=True,
        config_json={
            "bot_token": "$secret:COMMS_SLACK_BOT_TOKEN_MY_SLACK",
            "signing_secret": "$secret:COMMS_SLACK_SIGNING_SECRET_MY_SLACK",
        },
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "user_id": "U12345"}
        mock_post.return_value = mock_response

        adapter = await manager._init_adapter(channel)

    assert isinstance(adapter, SlackAdapter)
    assert adapter._bot_token == "xoxb-scoped-token"
    assert adapter._signing_secret == "scoped-signing-secret"
    assert adapter._bot_user_id == "U12345"
    await adapter.shutdown()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_channel_skips_empty_secrets() -> None:
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
    assert secret_store.set.call_args.kwargs["name"] == "COMMS_SLACK_BOT_TOKEN_MY_SLACK"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_remove_channel_shuts_down_and_deletes() -> None:
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_remove_channel_not_found_noop() -> None:
    """remove_channel() reports not found only when no DB row exists."""
    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    with pytest.raises(ValueError, match="not found"):
        await manager.remove_channel("nonexistent")

    store.delete_channel.assert_not_called()


@pytest.mark.unit
async def test_remove_channel_deletes_inactive_db_row_by_name() -> None:
    channel = make_channel(enabled=False)
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    await manager.remove_channel("test-channel")

    store.get_channel_by_name.assert_called_once_with("test-channel")
    store.delete_channel.assert_called_once_with("chan-1")
    assert "test-channel" not in manager._adapters
    assert manager.get_channel_status("test-channel")["status"] == "not_found"


@pytest.mark.unit
def test_list_channels() -> None:
    """list_channels() returns all channels from DB."""
    channels = [make_channel("ch1"), make_channel("ch2", channel_id="chan-2")]
    store = make_store(channels)
    store.list_channels.return_value = channels
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    result = manager.list_channels()
    assert len(result) == 2
    store.list_channels.assert_called_with(enabled_only=False)


@pytest.mark.unit
def test_get_channel_status_active() -> None:
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


@pytest.mark.unit
def test_get_channel_status_inactive() -> None:
    """get_channel_status() returns inactive for DB-only channel."""
    channel = make_channel()
    store = make_store()
    store.list_channels.return_value = [channel]
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    status = manager.get_channel_status("test-channel")
    assert status["status"] == "inactive"
    assert status["active"] is False


@pytest.mark.unit
def test_get_channel_status_not_found() -> None:
    """get_channel_status() returns not_found for unknown channel."""
    store = make_store()
    store.list_channels.return_value = []
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    status = manager.get_channel_status("ghost-channel")
    assert status["status"] == "not_found"
    assert status["active"] is False


@pytest.mark.unit
def test_get_channel_delegates_to_store() -> None:
    """get_channel() delegates to store.get_channel()."""
    channel = make_channel()
    store = make_store()
    store.get_channel.return_value = channel
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    result = manager.get_channel("chan-1")
    assert result == channel
    store.get_channel.assert_called_once_with("chan-1")


@pytest.mark.unit
def test_get_channel_returns_none_for_missing() -> None:
    """get_channel() returns None when channel doesn't exist."""
    store = make_store()
    store.get_channel.return_value = None
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    result = manager.get_channel("nonexistent")
    assert result is None


@pytest.mark.unit
async def test_update_channel_delegates_to_store() -> None:
    """update_channel() delegates to store and sets updated_at."""
    channel = make_channel()
    store = make_store()
    store.update_channel.return_value = channel
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    with patch("gobby.communications.manager.get_adapter_class", return_value=None):
        result = await manager.update_channel(channel)

    assert result == channel
    store.update_channel.assert_called_once_with(channel)
    # updated_at should be refreshed
    assert channel.updated_at != "2024-01-01T00:00:00"


@pytest.mark.unit
async def test_update_channel_stores_changed_secrets() -> None:
    channel = make_channel(channel_type="slack", name="my-slack")
    store = make_store()
    store.update_channel.return_value = channel
    secret_store = make_secret_store()
    manager = CommunicationsManager(make_config(), store, secret_store, MagicMock())

    with patch("gobby.communications.manager.get_adapter_class", return_value=None):
        result = await manager.update_channel(channel, secrets={"bot_token": "new-token"})

    assert result == channel
    secret_store.set.assert_called_once_with(
        name="COMMS_SLACK_BOT_TOKEN_MY_SLACK",
        plaintext_value="new-token",
        category="integration",
        description="slack channel 'my-slack': bot_token",
    )
    assert channel.config_json["bot_token"] == "$secret:COMMS_SLACK_BOT_TOKEN_MY_SLACK"


@pytest.mark.unit
async def test_update_channel_disable_stops_runtime_traffic() -> None:
    channel = make_channel()
    store = make_store([channel])
    store.update_channel.return_value = channel
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._polling_manager = MagicMock()
    adapter = make_adapter(supports_polling=True)
    manager._adapters[channel.name] = adapter
    manager._channel_by_name[channel.name] = channel

    channel.enabled = False

    result = await manager.update_channel(channel)

    assert result == channel
    manager._polling_manager.stop_polling.assert_called_once_with(channel.name)
    adapter.shutdown.assert_awaited_once()
    assert channel.name not in manager._adapters
    assert channel.name not in manager._channel_by_name
    assert manager.channel_to_dict(channel)["active"] is False


@pytest.mark.unit
async def test_update_channel_enabled_reinitializes_and_refreshes_runtime_state() -> None:
    channel = make_channel(config_json={"rate_limit_per_minute": 7, "burst": 3})
    store = make_store([channel])
    store.update_channel.return_value = channel
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._rate_limiter = MagicMock()
    old_adapter = make_adapter()
    new_adapter = make_adapter()
    manager._adapters[channel.name] = old_adapter
    manager._channel_by_name[channel.name] = channel
    mock_adapter_cls = MagicMock(return_value=new_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        result = await manager.update_channel(channel)

    assert result == channel
    old_adapter.shutdown.assert_awaited_once()
    new_adapter.initialize.assert_awaited_once()
    assert manager._adapters[channel.name] == new_adapter
    assert manager._channel_by_name[channel.name] == channel
    manager._rate_limiter.configure_channel.assert_called_once_with("chan-1", 7, 3)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_injects_platform_destination() -> None:
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_preserves_caller_platform_destination() -> None:
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_no_platform_destination_without_config() -> None:
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_injects_conversation_reference_destination() -> None:
    """send_message() injects Teams conversation reference destination fields."""
    channel = make_channel(channel_type="teams", config_json={})
    identity = CommsIdentity(
        id="identity-1",
        channel_id=channel.id,
        external_user_id="teams-user-1",
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
        session_id="session-abc",
        metadata_json={
            "conversation_reference": {
                "conversation_id": "teams-conversation-1",
                "service_url": "https://smba.trafficmanager.net/apis/",
            }
        },
    )
    store = make_store([channel])
    store.list_identities.return_value = [identity]
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter(channel_type="teams")
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    msg = await manager.send_message("test-channel", "Hello!", session_id="session-abc")

    assert msg.channel_id == channel.id
    assert msg.metadata_json["platform_destination"] == "teams-conversation-1"
    assert msg.metadata_json["service_url"] == "https://smba.trafficmanager.net/apis/"
    assert (
        msg.metadata_json["conversation_reference"]
        == identity.metadata_json["conversation_reference"]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_telegram_inbound_session_reply_resolves_chat_destination() -> None:
    """A session auto-created from Telegram inbound can send back to the originating chat."""
    channel = make_channel(
        channel_type="telegram",
        config_json={"allow_from": ["1111111"]},
    )
    identities: list[CommsIdentity] = []
    store = make_store([channel])
    store.get_identity_by_external.side_effect = lambda channel_id, external_user_id: next(
        (
            identity
            for identity in identities
            if identity.channel_id == channel_id and identity.external_user_id == external_user_id
        ),
        None,
    )

    def create_identity(identity: CommsIdentity) -> CommsIdentity:
        identities.append(identity)
        return identity

    store.create_identity.side_effect = create_identity
    store.list_identities.side_effect = lambda channel_id=None: [
        identity
        for identity in identities
        if channel_id is None or identity.channel_id == channel_id
    ]

    session_store = MagicMock()
    session_store.register.return_value = MagicMock(id="telegram-session-1")
    manager = CommunicationsManager(
        make_config(),
        store,
        make_secret_store(),
        session_store,
    )
    mock_adapter = make_adapter(channel_type="telegram")

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()

    inbound = TelegramAdapter().parse_webhook(
        {
            "update_id": 10000,
            "message": {
                "message_id": 1365,
                "from": {"id": 1111111, "is_bot": False, "username": "testuser"},
                "chat": {"id": 2222222, "type": "private"},
                "date": 1441645532,
                "text": "hello",
            },
        },
        {},
    )
    stored = await manager.handle_inbound_messages("test-channel", inbound)

    assert stored[0].session_id == "telegram-session-1"

    reply = await manager.send_message(
        "test-channel",
        "Hello!",
        session_id="telegram-session-1",
    )

    assert reply.status == "sent"
    sent_message = mock_adapter.send_message.await_args.args[0]
    assert sent_message.metadata_json["platform_destination"] == "2222222"
    assert sent_message.metadata_json["conversation_reference"] == {
        "conversation_id": "2222222",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_propagates_thread_id() -> None:
    """send_message() should include platform_thread_id from thread map."""
    channel = make_channel(webhook_secret=None)
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter.send_message.return_value = "out-msg-1"
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    manager._thread_manager.track_thread("chan-1", "session-123", "thread-456")

    msg = await manager.send_message("test-channel", "Hello reply", session_id="session-123")

    assert msg.platform_thread_id == "thread-456"
    assert msg.status == "sent"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_explicit_thread_id_overrides_tracked_thread() -> None:
    channel = make_channel(webhook_secret=None)
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    mock_adapter = make_adapter()

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()

    manager._thread_manager.track_thread("chan-1", "session-123", "tracked-thread")
    message = await manager.send_message(
        "test-channel",
        "Explicit reply",
        session_id="session-123",
        metadata={"thread_id": "explicit-thread"},
    )

    assert message.platform_thread_id == "explicit-thread"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_deduplicates_platform_message_and_returns_it_for_ack() -> None:
    channel = make_channel()
    store = make_store([channel])
    store.get_message_by_platform_id.return_value = CommsMessage(
        id="stored-message",
        channel_id=channel.id,
        direction="inbound",
        content="already handled",
        platform_message_id="platform-message-1",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._channel_by_name[channel.name] = channel
    manager._identity_manager = MagicMock()
    manager.event_callback = AsyncMock()

    duplicate = CommsMessage(
        id="duplicate-delivery",
        channel_id="platform-chat-1",
        direction="inbound",
        content="already handled",
        platform_message_id="platform-message-1",
        identity_id="platform-user-1",
        metadata_json={"telegram_update_id": 501},
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )

    handled = await manager.handle_inbound_messages(channel.name, [duplicate])

    assert handled == [store.get_message_by_platform_id.return_value]
    store.get_message_by_platform_id.assert_called_once_with(
        channel.name, duplicate.platform_message_id
    )
    store.create_message.assert_not_called()
    manager._identity_manager.resolve_identity.assert_not_called()
    manager.event_callback.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_deduplicates_reaction_before_action_dispatch() -> None:
    channel = make_channel()
    store = make_store([channel])
    store.get_message_by_platform_id.return_value = CommsMessage(
        id="stored-reaction",
        channel_id=channel.id,
        direction="inbound",
        content="👍",
        content_type="reaction",
        platform_message_id="reaction:501:message-1",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._channel_by_name[channel.name] = channel
    manager.reaction_handler = AsyncMock()

    duplicate = CommsMessage(
        id="duplicate-reaction",
        channel_id="platform-chat-1",
        direction="inbound",
        content="👍",
        content_type="reaction",
        platform_message_id="reaction:501:message-1",
        identity_id="platform-user-1",
        metadata_json={
            "reaction_target_message_id": "message-1",
            "telegram_update_id": 501,
        },
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )

    handled = await manager.handle_inbound_messages(channel.name, [duplicate])

    assert handled == [store.get_message_by_platform_id.return_value]
    manager.reaction_handler.handle_reaction.assert_not_awaited()
    store.create_message.assert_not_called()


@pytest.mark.unit
async def test_handle_inbound_populates_thread_map_and_handles_reactions() -> None:
    """handle_inbound_messages() should populate thread map and dispatch reactions."""
    channel = make_channel(webhook_secret=None)
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_identity = CommsIdentity(
        id="id-1",
        channel_id="chan-1",
        external_user_id="user-1",
        session_id="session-123",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )

    manager._identity_manager = MagicMock()
    manager._identity_manager.resolve_inbound_identity = MagicMock(
        return_value=IdentityResolution(identity=mock_identity, session_id="session-123")
    )

    manager.reaction_handler = AsyncMock()

    inbound_msg = CommsMessage(
        id="msg-1",
        channel_id="chan-1",
        direction="inbound",
        content="Hello",
        platform_thread_id="thread-456",
        created_at="2024-01-01T00:00:00Z",
        identity_id="user-1",
    )

    rxn_msg = CommsMessage(
        id="rxn-1",
        channel_id="chan-1",
        direction="inbound",
        content="+1",
        platform_message_id="reaction:501:msg-123",
        content_type="reaction",
        created_at="2024-01-01T00:00:00Z",
        identity_id="user-1",
        metadata_json={"reaction_target_message_id": "msg-123"},
    )

    # Needs to be dict-like so _channel_by_name works; manager.start() does that.
    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)
    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    handled = await manager.handle_inbound_messages(
        "test-channel",
        [inbound_msg, rxn_msg],
    )

    assert manager._thread_manager._thread_map[("chan-1", "session-123")] == "thread-456"
    assert handled == [inbound_msg, rxn_msg]
    store.create_message.assert_any_call(rxn_msg)

    # reaction should have called handler
    manager.reaction_handler.handle_reaction.assert_awaited_once_with(
        "test-channel", "msg-123", "+1", "user-1"
    )
    assert manager.reaction_handler.handle_reaction.await_count == 1
    assert manager.reaction_handler.handle_reaction.await_args is not None


@pytest.mark.unit
def test_thread_map_lru_eviction_order() -> None:
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


@pytest.mark.unit
def test_thread_map_move_to_end_on_track() -> None:
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


@pytest.mark.unit
def test_event_subscription_crud_validates_scope_and_invalidates_cache() -> None:
    """Manager CRUD owns subscription identity, validation, and cache invalidation."""
    channel = make_channel(name="Telegram", channel_id="cccccccc-1111-4ccc-8ccc-cccccccc0001")
    store = make_store([channel])
    rules: dict[str, CommsRoutingRule] = {}
    store.get_channel.side_effect = lambda channel_id: (
        channel if channel_id == channel.id else None
    )
    store.create_routing_rule.side_effect = lambda rule: rules.setdefault(rule.id, rule)
    store.get_routing_rule.side_effect = rules.get
    store.list_routing_rules.side_effect = lambda **_filters: list(rules.values())
    store.update_routing_rule.side_effect = lambda rule: rules.setdefault(rule.id, rule)
    store.delete_routing_rule.side_effect = rules.pop
    session_store = MagicMock()
    session_store.get.return_value = MagicMock(project_id="project-1")
    manager = CommunicationsManager(make_config(), store, make_secret_store(), session_store)
    manager._router._rules_cache = []

    created = manager.create_event_subscription(
        name=" Agent pauses ",
        channel="Telegram",
        event_pattern=" session.agent.paused ",
        project_id="project-1",
        global_scope=False,
        session_id="session-1",
        priority=10,
    )

    assert str(uuid.UUID(created.id)) == created.id
    assert created.name == "Agent pauses"
    assert created.event_pattern == "session.agent.paused"
    assert created.channel_id == channel.id
    assert created.project_id == "project-1"
    assert created.session_id == "session-1"
    assert created.config_json == {}
    assert created.created_at == created.updated_at
    assert all(args.args != ("Telegram",) for args in store.get_channel.call_args_list)
    assert manager._router._rules_cache is None

    manager._router._rules_cache = [created]
    assert manager.get_event_subscription(created.id) is created
    assert manager.list_event_subscriptions(
        channel=channel.id,
        project_id="project-1",
        enabled=False,
        event_pattern="session.agent.paused",
    ) == [created]
    store.list_routing_rules.assert_called_once_with(
        channel_id=channel.id,
        project_id="project-1",
        global_scope=None,
        enabled=False,
        event_pattern="session.agent.paused",
    )

    updated = manager.update_event_subscription(
        created.id,
        name="Expired agents",
        event_pattern="session.agent.expired",
        session_id=None,
        priority=20,
        enabled=False,
    )
    assert updated.name == "Expired agents"
    assert updated.event_pattern == "session.agent.expired"
    assert updated.session_id is None
    assert updated.priority == 20
    assert updated.enabled is False
    assert manager._router._rules_cache is None

    public = manager.event_subscription_to_dict(updated)
    assert public["channel_id"] == channel.id
    assert public["channel_name"] == "Telegram"
    assert public["scope"] == {"kind": "project", "project_id": "project-1"}
    assert public["created_at"] == datetime_to_local_iso(updated.created_at)
    assert public["updated_at"] == datetime_to_local_iso(updated.updated_at)
    assert "config_json" not in public

    manager._router._rules_cache = [updated]
    manager.delete_event_subscription(created.id)
    assert rules == {}
    assert manager._router._rules_cache is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"name": " "}, "Subscription name is required"),
        ({"event_pattern": " "}, "Event pattern is required"),
        ({"channel": "missing"}, "Channel 'missing' not found"),
        (
            {"project_id": None, "global_scope": False},
            "Project scope or explicit global scope is required",
        ),
        (
            {"project_id": "project-1", "global_scope": True},
            "Choose either project scope or global scope",
        ),
        (
            {"project_id": None, "global_scope": True, "session_id": "session-1"},
            "Global subscriptions cannot be session-scoped",
        ),
    ],
)
@pytest.mark.unit
def test_event_subscription_create_rejects_invalid_contract(
    overrides: dict[str, object],
    message: str,
) -> None:
    channel = make_channel(name="Telegram", channel_id="cccccccc-1111-4ccc-8ccc-cccccccc0001")
    store = make_store([channel])
    store.get_channel.return_value = None
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    kwargs: dict[str, object] = {
        "name": "Agent pauses",
        "channel": "Telegram",
        "event_pattern": "session.agent.paused",
        "project_id": "project-1",
        "global_scope": False,
    }
    kwargs.update(overrides)

    with pytest.raises(ValueError, match=message):
        cast(Any, manager.create_event_subscription)(**kwargs)


@pytest.mark.unit
def test_event_subscription_session_must_belong_to_selected_project() -> None:
    channel = make_channel(name="Telegram", channel_id="cccccccc-1111-4ccc-8ccc-cccccccc0001")
    store = make_store([channel])
    store.get_channel.return_value = None
    session_store = MagicMock()
    session_store.get.return_value = MagicMock(project_id="project-2")
    manager = CommunicationsManager(make_config(), store, make_secret_store(), session_store)

    with pytest.raises(ValueError, match="Session does not belong"):
        manager.create_event_subscription(
            name="Agent pauses",
            channel="Telegram",
            event_pattern="session.agent.paused",
            project_id="project-1",
            global_scope=False,
            session_id="session-1",
        )


def _telegram_group_message(*, sender_id: str, mentioned: bool) -> CommsMessage:
    return CommsMessage(
        id=f"group-{sender_id}-{mentioned}",
        channel_id="",
        direction="inbound",
        content="ambient group discussion",
        identity_id=sender_id,
        metadata_json={
            "chat_id": "-100123",
            "platform_channel_id": "-100123",
            "conversation_type": "supergroup",
            "mentioned": mentioned,
            "external_username": sender_id,
        },
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mentioned", "expected_passive"),
    [(False, True), (True, False)],
)
@pytest.mark.unit
async def test_configured_group_message_is_classified_before_persistence(
    mentioned: bool,
    expected_passive: bool,
) -> None:
    channel = make_channel(
        channel_type="telegram",
        config_json={
            "allow_from": ["owner"],
            "group_policy": "allowlist",
            "groups": {"-100123": {}},
            "require_mention": True,
        },
    )
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._channel_by_name[channel.name] = channel
    identity = CommsIdentity(
        id="identity-owner",
        channel_id=channel.id,
        external_user_id="owner",
        session_id="group-session",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    object.__setattr__(
        manager._identity_manager,
        "resolve_inbound_identity",
        MagicMock(return_value=IdentityResolution(identity=identity, session_id="group-session")),
    )
    message = _telegram_group_message(sender_id="owner", mentioned=mentioned)

    handled = await manager.handle_inbound_messages(channel.name, [message])

    assert len(handled) == 1
    assert handled[0].metadata_json["passive_context"] is expected_passive
    store.create_message.assert_called_once_with(message)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_group_allowlist_rejection_happens_before_identity_or_persistence() -> None:
    channel = make_channel(
        channel_type="telegram",
        config_json={
            "allow_from": ["owner"],
            "group_policy": "allowlist",
            "groups": {"-100123": {}},
            "require_mention": True,
        },
    )
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._channel_by_name[channel.name] = channel
    resolve_identity = MagicMock()
    object.__setattr__(
        manager._identity_manager,
        "resolve_inbound_identity",
        resolve_identity,
    )
    message = _telegram_group_message(sender_id="stranger", mentioned=True)

    handled = await manager.handle_inbound_messages(channel.name, [message])

    assert handled == [message]
    resolve_identity.assert_not_called()
    store.create_message.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_telegram_reply_targets_originating_session_with_shared_chat() -> None:
    channel = make_channel(
        channel_type="telegram",
        config_json={"allow_from": ["1111111"]},
    )
    store = make_store([channel])
    created_at = datetime(2024, 1, 1, tzinfo=UTC)
    outbound_by_platform_id = {
        "1001": CommsMessage(
            id="outbound-a",
            channel_id=channel.id,
            direction="outbound",
            content="Session A",
            platform_message_id="1001",
            session_id="session-a",
            metadata_json={"platform_destination": "2222222"},
            created_at=created_at,
        ),
        "1002": CommsMessage(
            id="outbound-b",
            channel_id=channel.id,
            direction="outbound",
            content="Session B",
            platform_message_id="1002",
            session_id="session-b",
            metadata_json={"platform_destination": "2222222"},
            created_at=created_at,
        ),
    }

    def get_message_by_platform_id(
        channel_name: str,
        platform_message_id: str,
        *,
        platform_destination: str | None = None,
    ) -> CommsMessage | None:
        if channel_name != channel.name or platform_destination != "2222222":
            return None
        return outbound_by_platform_id.get(platform_message_id)

    store.get_message_by_platform_id.side_effect = get_message_by_platform_id
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    identity = CommsIdentity(
        id="identity-1",
        channel_id=channel.id,
        external_user_id="1111111",
        session_id="session-b",
        created_at=created_at,
        updated_at=created_at,
    )
    manager._identity_manager = MagicMock()
    manager._identity_manager.resolve_inbound_identity.return_value = IdentityResolution(
        identity=identity,
        session_id="session-b",
    )
    manager.event_callback = AsyncMock()
    mock_adapter = make_adapter(channel_type="telegram")

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()

    inbound = TelegramAdapter().parse_webhook(
        {
            "update_id": 10001,
            "message": {
                "message_id": 1003,
                "from": {"id": 1111111, "is_bot": False, "username": "testuser"},
                "chat": {"id": 2222222, "type": "private"},
                "date": 1441645532,
                "text": "Reply for session A",
                "reply_to_message": {"message_id": 1001},
            },
        },
        {},
    )
    stored = await manager.handle_inbound_messages(channel.name, inbound)

    assert len(stored) == 1
    assert stored[0].content == "Reply for session A"
    assert stored[0].metadata_json["reply_to_message_id"] == "1001"
    assert stored[0].identity_id == identity.id
    assert stored[0].session_id == "session-a"
    store.get_message_by_platform_id.assert_any_call(
        channel.name,
        "1001",
        platform_destination="2222222",
    )
    manager.event_callback.assert_awaited_once_with(
        "comms.message_received",
        message=stored[0],
    )
