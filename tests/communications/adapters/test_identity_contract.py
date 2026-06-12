"""Adapter identity contract conformance tests."""

from __future__ import annotations

from collections.abc import Callable
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import gobby.communications.adapters.email as email_adapter_module
from gobby.communications.adapters.discord import DiscordAdapter
from gobby.communications.adapters.email import EmailAdapter
from gobby.communications.adapters.gobby_chat import GobbyChatAdapter
from gobby.communications.adapters.slack import SlackAdapter
from gobby.communications.adapters.sms import SMSAdapter
from gobby.communications.adapters.teams import TeamsAdapter
from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.models import CommsMessage


def _assert_identity_contract(
    message: CommsMessage,
    *,
    identity_id: str,
    external_username: str,
) -> None:
    assert message.identity_id == identity_id
    assert message.metadata_json["external_username"] == external_username


def _slack_message() -> CommsMessage:
    messages = SlackAdapter().parse_webhook(
        {
            "type": "event_callback",
            "event": {
                "type": "message",
                "text": "hello",
                "user": "U123",
                "username": "alice",
                "channel": "C123",
                "ts": "123.456",
            },
        },
        {},
    )
    return messages[0]


def _discord_message() -> CommsMessage:
    messages = DiscordAdapter().parse_webhook(
        {
            "id": "msg-1",
            "channel_id": "channel-1",
            "content": "hello",
            "member": {"user": {"id": "D123", "username": "alice_discord"}},
        },
        {},
    )
    return messages[0]


def _sms_message() -> CommsMessage:
    messages = SMSAdapter().parse_webhook(
        {"From": "+15551234567", "To": "+15557654321", "Body": "hello", "MessageSid": "SM1"},
        {},
    )
    return messages[0]


def _teams_message() -> CommsMessage:
    messages = TeamsAdapter().parse_webhook(
        {
            "type": "message",
            "id": "teams-msg-1",
            "text": "hello",
            "from": {"id": "teams-user-1", "name": "Teams Alice"},
            "conversation": {"id": "teams-conv-1", "tenantId": "tenant-1"},
        },
        {},
    )
    return messages[0]


def _telegram_message() -> CommsMessage:
    messages = TelegramAdapter().parse_webhook(
        {
            "update_id": 1,
            "message": {
                "message_id": 42,
                "from": {"id": 1111111, "is_bot": False, "username": "telegram_alice"},
                "chat": {"id": 2222222, "type": "private"},
                "text": "hello",
            },
        },
        {},
    )
    return messages[0]


def _gobby_chat_message() -> CommsMessage:
    message = GobbyChatAdapter().parse_inbound(
        {
            "type": "message",
            "message_id": "chat-msg-1",
            "conversation_id": "session-1",
            "user_id": "gobby-user-1",
            "username": "Gobby Alice",
            "content": "hello",
        }
    )
    assert message is not None
    return message


@pytest.mark.parametrize(
    ("adapter_name", "message_factory", "identity_id", "external_username"),
    [
        ("slack", _slack_message, "U123", "alice"),
        ("discord", _discord_message, "D123", "alice_discord"),
        ("sms", _sms_message, "+15551234567", "+15551234567"),
        ("teams", _teams_message, "teams-user-1", "Teams Alice"),
        ("telegram", _telegram_message, "1111111", "telegram_alice"),
        ("gobby_chat", _gobby_chat_message, "gobby-user-1", "Gobby Alice"),
    ],
)
def test_inbound_adapter_messages_populate_identity_contract(
    adapter_name: str,
    message_factory: Callable[[], CommsMessage],
    identity_id: str,
    external_username: str,
) -> None:
    message = message_factory()

    _assert_identity_contract(
        message,
        identity_id=identity_id,
        external_username=external_username,
    )


@pytest.mark.asyncio
async def test_email_poll_populates_identity_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(email_adapter_module, "HAS_IMAP", True)
    adapter = EmailAdapter()
    adapter._ensure_imap_connected = AsyncMock()  # type: ignore[method-assign]
    adapter._imap_client = AsyncMock()

    raw_message = EmailMessage()
    raw_message["Message-ID"] = "email-msg-1"
    raw_message["From"] = "Email Alice <alice@example.com>"
    raw_message["Subject"] = "Hello"
    raw_message.set_content("hello")

    adapter._imap_client.search.return_value = SimpleNamespace(
        result="OK",
        lines=[b"1"],
    )
    adapter._imap_client.fetch.return_value = SimpleNamespace(
        result="OK",
        lines=[b"1 (RFC822)", bytes(raw_message)],
    )
    adapter._imap_client.store.return_value = SimpleNamespace(result="OK", lines=[])

    messages = await adapter.poll()

    assert len(messages) == 1
    _assert_identity_contract(
        messages[0],
        identity_id="alice@example.com",
        external_username="Email Alice",
    )
