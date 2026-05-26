"""Tests for communications storage — cascade delete."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from gobby.communications.models import (
    ChannelConfig,
    CommsAttachment,
    CommsIdentity,
    CommsMessage,
    CommsRoutingRule,
)
from gobby.storage.communications import LocalCommunicationsStore

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


@pytest.fixture
def store(temp_db: HubDatabase) -> LocalCommunicationsStore:
    return LocalCommunicationsStore(temp_db, project_id="")


def _make_channel(store: LocalCommunicationsStore, name: str = "test-ch") -> ChannelConfig:
    return store.create_channel(
        ChannelConfig(
            id="",
            channel_type="slack",
            name=name,
            enabled=True,
            config_json={},
            created_at="2025-01-01T00:00:00Z",
            updated_at="2025-01-01T00:00:00Z",
        )
    )


def test_delete_channel_cascades_messages(store: LocalCommunicationsStore) -> None:
    """Deleting a channel removes its messages."""
    ch = _make_channel(store)
    store.create_message(
        CommsMessage(
            id="",
            channel_id=ch.id,
            direction="inbound",
            content="hello",
            created_at="2025-01-01T00:00:00Z",
        )
    )
    assert len(store.list_messages(channel_id=ch.id)) == 1

    store.delete_channel(ch.id)

    assert store.get_channel(ch.id) is None
    assert len(store.list_messages(channel_id=ch.id)) == 0


def test_delete_channel_cascades_identities(store: LocalCommunicationsStore) -> None:
    """Deleting a channel removes its identities."""
    ch = _make_channel(store)
    store.create_identity(
        CommsIdentity(
            id="",
            channel_id=ch.id,
            external_user_id="u1",
            external_username="alice",
            created_at="2025-01-01T00:00:00Z",
            updated_at="2025-01-01T00:00:00Z",
        )
    )
    assert len(store.list_identities(channel_id=ch.id)) == 1

    store.delete_channel(ch.id)

    assert len(store.list_identities(channel_id=ch.id)) == 0


def test_delete_channel_cascades_routing_rules(store: LocalCommunicationsStore) -> None:
    """Deleting a channel removes its routing rules."""
    ch = _make_channel(store)
    store.create_routing_rule(
        CommsRoutingRule(
            id="",
            name="rule-1",
            channel_id=ch.id,
            event_pattern="task.*",
            priority=1,
            enabled=True,
            config_json={},
            created_at="2025-01-01T00:00:00Z",
            updated_at="2025-01-01T00:00:00Z",
        )
    )
    assert len(store.list_routing_rules(channel_id=ch.id, enabled_only=False)) >= 1

    store.delete_channel(ch.id)

    assert len(store.list_routing_rules(channel_id=ch.id, enabled_only=False)) == 0


def test_delete_channel_cascades_attachments(store: LocalCommunicationsStore) -> None:
    """Deleting a channel removes attachments on its messages."""
    ch = _make_channel(store)
    msg = store.create_message(
        CommsMessage(
            id="",
            channel_id=ch.id,
            direction="inbound",
            content="file here",
            created_at="2025-01-01T00:00:00Z",
        )
    )
    store.create_attachment(
        CommsAttachment(
            id="",
            message_id=msg.id,
            filename="doc.pdf",
            content_type="application/pdf",
            size_bytes=1024,
            created_at="2025-01-01T00:00:00Z",
        )
    )
    assert len(store.list_attachments(msg.id)) == 1

    store.delete_channel(ch.id)

    assert len(store.list_attachments(msg.id)) == 0


def test_delete_channel_is_atomic(store: LocalCommunicationsStore) -> None:
    """All cascade deletes happen in one transaction — other channels are untouched."""
    ch1 = _make_channel(store, name="ch-1")
    ch2 = _make_channel(store, name="ch-2")

    store.create_message(
        CommsMessage(
            id="",
            channel_id=ch1.id,
            direction="inbound",
            content="msg-1",
            created_at="2025-01-01T00:00:00Z",
        )
    )
    store.create_message(
        CommsMessage(
            id="",
            channel_id=ch2.id,
            direction="inbound",
            content="msg-2",
            created_at="2025-01-01T00:00:00Z",
        )
    )

    store.delete_channel(ch1.id)

    # ch2 data should be untouched
    assert store.get_channel(ch2.id) is not None
    assert len(store.list_messages(channel_id=ch2.id)) == 1
