"""Telegram first-contact binding and pre-persistence access tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.communications.identities import IdentityResolution
from gobby.communications.manager import CommunicationsManager
from gobby.communications.models import ChannelConfig, CommsIdentity, CommsMessage
from gobby.communications.telegram_access import (
    allowed_senders,
    is_deliberate_start,
    is_telegram_dm,
    telegram_dm_sender,
)
from gobby.config.communications import ChannelDefaults, CommunicationsConfig


def _channel(*, allow_from: list[str] | None = None) -> ChannelConfig:
    config_json: dict[str, object] = {"responder": {"enabled": True}}
    if allow_from is not None:
        config_json["allow_from"] = allow_from
    return ChannelConfig(
        id="telegram-channel",
        channel_type="telegram",
        name="telegram",
        enabled=True,
        config_json=config_json,
        created_at=datetime(2026, 7, 24, tzinfo=UTC),
        updated_at=datetime(2026, 7, 24, tzinfo=UTC),
    )


def _message(
    sender_id: str,
    content: str,
    *,
    conversation_type: str = "private",
) -> CommsMessage:
    return CommsMessage(
        id=f"message-{sender_id}-{content}",
        channel_id="platform-chat",
        direction="inbound",
        content=content,
        identity_id=sender_id,
        platform_message_id=f"platform-{sender_id}-{content}",
        metadata_json={
            "conversation_type": conversation_type,
            "platform_channel_id": f"chat-{sender_id}",
            "telegram_update_id": 100,
        },
        created_at=datetime(2026, 7, 24, tzinfo=UTC),
    )


def _manager(
    channel: ChannelConfig,
) -> tuple[CommunicationsManager, MagicMock, MagicMock, AsyncMock]:
    store = MagicMock()
    store.get_channel.return_value = channel
    store.update_channel.side_effect = lambda updated: updated
    store.get_message_by_platform_id.return_value = None
    store.create_message.side_effect = lambda message: message
    store.get_routing_rules.return_value = []

    manager = CommunicationsManager(
        CommunicationsConfig(
            enabled=True,
            channel_defaults=ChannelDefaults(rate_limit_per_minute=60, burst=10),
        ),
        store,
        MagicMock(),
        MagicMock(),
    )
    manager._channel_by_name[channel.name] = channel
    adapter = MagicMock()
    adapter.download_inbound_attachments = AsyncMock(return_value=[])
    manager._adapters[channel.name] = adapter

    identity = CommsIdentity(
        id="identity",
        channel_id=channel.id,
        external_user_id="owner",
        session_id="session",
        created_at=datetime(2026, 7, 24, tzinfo=UTC),
        updated_at=datetime(2026, 7, 24, tzinfo=UTC),
    )
    identity_resolver = MagicMock(
        return_value=IdentityResolution(
            identity=identity,
            session_id="session",
        )
    )
    manager._identity_manager = MagicMock()
    manager._identity_manager.resolve_inbound_identity = identity_resolver
    event_callback = AsyncMock()
    manager.event_callback = event_callback
    return manager, store, identity_resolver, event_callback


def test_telegram_access_helpers_require_exact_private_start() -> None:
    channel = _channel()
    private_start = _message("123", "/start")

    assert is_telegram_dm(channel, private_start) is True
    assert telegram_dm_sender(channel, private_start) == "123"
    assert allowed_senders({"allow_from": ["123", 456, True, None]}) == {"123", "456"}
    assert is_deliberate_start("/start") is True
    assert is_deliberate_start("/start@GobbyAIBot") is True
    assert is_deliberate_start("/start payload") is False
    assert is_deliberate_start("hello") is False


@pytest.mark.asyncio
async def test_unregistered_telegram_dm_is_acknowledged_without_persistence() -> None:
    channel = _channel(allow_from=["owner"])
    manager, store, identity_resolver, event_callback = _manager(channel)
    message = _message("stranger", "hello")

    handled = await manager.handle_inbound_messages(channel.name, [message])

    assert handled == [message]
    store.create_message.assert_not_called()
    store.update_channel.assert_not_called()
    identity_resolver.assert_not_called()
    event_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_private_start_binds_sender_before_identity_and_message() -> None:
    channel = _channel()
    manager, store, identity_resolver, event_callback = _manager(channel)
    message = _message("owner", "/start")

    handled = await manager.handle_inbound_messages(channel.name, [message])

    assert handled == [message]
    assert channel.config_json["allow_from"] == ["owner"]
    assert message.metadata_json["telegram_first_contact_bound"] is True
    updated = store.update_channel.call_args.args[0]
    assert updated.config_json["allow_from"] == ["owner"]
    identity_resolver.assert_called_once()
    store.create_message.assert_called_once_with(message)
    event_callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_start_cannot_claim_empty_telegram_channel() -> None:
    channel = _channel()
    manager, store, _identity_resolver, _event_callback = _manager(channel)
    message = _message("stranger", "/start", conversation_type="group")

    assert await manager.admit_inbound_message(channel, message) is True
    assert "allow_from" not in channel.config_json
    store.update_channel.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_first_starts_bind_exactly_one_sender() -> None:
    channel = _channel()
    manager, store, _identity_resolver, _event_callback = _manager(channel)
    first = _message("first", "/start")
    second = _message("second", "/start")

    admitted = await asyncio.gather(
        manager.admit_inbound_message(channel, first),
        manager.admit_inbound_message(channel, second),
    )

    assert admitted.count(True) == 1
    assert admitted.count(False) == 1
    assert channel.config_json["allow_from"] in (["first"], ["second"])
    store.update_channel.assert_called_once()
