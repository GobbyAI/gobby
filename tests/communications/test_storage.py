"""Tests for local communications store."""

import pytest

from gobby.communications.models import (
    ChannelConfig,
    CommsIdentity,
    CommsMessage,
    CommsRoutingRule,
)
from gobby.storage.communications import LocalCommunicationsStore
from gobby.storage.hub.protocol import HubDatabase


@pytest.fixture
def comms_store(temp_db: HubDatabase) -> LocalCommunicationsStore:
    """Fixture for communications store."""
    return LocalCommunicationsStore(temp_db, project_id="00000000-0000-0000-0000-000000000000")


def test_channel_crud(comms_store: LocalCommunicationsStore) -> None:
    """Test full CRUD lifecycle for channels."""
    # Create
    channel = ChannelConfig(
        id="",
        channel_type="test",
        name="Test Channel",
        enabled=True,
        config_json={"api_key": "secret"},
        webhook_secret="wh_secret",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    saved = comms_store.create_channel(channel)
    assert saved.id.startswith("cc-")
    assert saved.name == "Test Channel"

    # Read
    fetched = comms_store.get_channel(saved.id)
    assert fetched is not None
    assert fetched.name == "Test Channel"
    assert fetched.config_json == {"api_key": "secret"}
    assert fetched.webhook_secret == "wh_secret"

    fetched_by_name = comms_store.get_channel_by_name("Test Channel")
    assert fetched_by_name is not None
    assert fetched_by_name.id == saved.id

    # List
    channels = comms_store.list_channels(enabled_only=True)
    assert len(channels) == 1
    assert channels[0].id == saved.id

    # Update
    saved.name = "Updated Channel"
    saved.enabled = False
    comms_store.update_channel(saved)

    updated = comms_store.get_channel(saved.id)
    assert updated is not None
    assert updated.name == "Updated Channel"
    assert not updated.enabled

    # List enabled
    enabled_channels = comms_store.list_channels(enabled_only=True)
    assert len(enabled_channels) == 0

    # Delete
    comms_store.delete_channel(saved.id)
    assert comms_store.get_channel(saved.id) is None


def test_identity_crud(comms_store: LocalCommunicationsStore) -> None:
    """Test full CRUD lifecycle for identities."""
    # Need a channel first because of FK
    channel = ChannelConfig(
        id="cc-test",
        channel_type="test",
        name="Test",
        enabled=True,
        config_json={},
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    comms_store.create_channel(channel)

    # Create
    identity = CommsIdentity(
        id="",
        channel_id="cc-test",
        external_user_id="user_123",
        external_username="testuser",
        session_id=None,
        project_id=None,  # Should use store's project_id
        metadata_json={"role": "admin"},
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    saved = comms_store.create_identity(identity)
    assert saved.id.startswith("ci-")
    assert saved.project_id == "00000000-0000-0000-0000-000000000000"

    # Read
    fetched = comms_store.get_identity(saved.id)
    assert fetched is not None
    assert fetched.external_username == "testuser"
    assert fetched.metadata_json == {"role": "admin"}

    fetched_ext = comms_store.get_identity_by_external("cc-test", "user_123")
    assert fetched_ext is not None
    assert fetched_ext.id == saved.id

    # List
    identities = comms_store.list_identities(channel_id="cc-test")
    assert len(identities) == 1

    identities_none = comms_store.list_identities(channel_id="other")
    assert len(identities_none) == 0

    # Update
    saved.external_username = "newuser"
    comms_store.update_identity(saved)

    updated = comms_store.get_identity(saved.id)
    assert updated is not None
    assert updated.external_username == "newuser"

    # Delete
    comms_store.delete_identity(saved.id)
    assert comms_store.get_identity(saved.id) is None


def test_message_crud(comms_store: LocalCommunicationsStore) -> None:
    """Test full CRUD lifecycle for messages."""
    # Create channel & identity
    comms_store.create_channel(
        ChannelConfig(
            id="cc-msg",
            channel_type="test",
            name="Msg",
            enabled=True,
            config_json={},
            created_at="2024",
            updated_at="2024",
        )
    )
    comms_store.create_identity(
        CommsIdentity(
            id="ci-msg",
            channel_id="cc-msg",
            external_user_id="u1",
            created_at="2024",
            updated_at="2024",
        )
    )

    # Create
    message = CommsMessage(
        id="",
        channel_id="cc-msg",
        identity_id="ci-msg",
        direction="inbound",
        content="Hello world",
        content_type="text",
        platform_message_id="msg_1",
        session_id=None,
        status="sent",
        metadata_json={"tokens": 10},
        created_at="2024-01-01T00:00:00Z",
    )
    saved = comms_store.create_message(message)
    assert saved.id.startswith("cm-")

    # Read
    fetched = comms_store.get_message(saved.id)
    assert fetched is not None
    assert fetched.content == "Hello world"
    assert fetched.direction == "inbound"

    # List (Filter and sort)
    messages = comms_store.list_messages(channel_id="cc-msg", direction="inbound")
    assert len(messages) == 1
    assert messages[0].id == saved.id

    messages_empty = comms_store.list_messages(session_id="other")
    assert len(messages_empty) == 0

    # Update status
    comms_store.update_message_status(saved.id, "delivered", "no error")
    updated = comms_store.get_message(saved.id)
    assert updated is not None
    assert updated.status == "delivered"
    assert updated.error == "no error"


def test_routing_rule_crud(comms_store: LocalCommunicationsStore) -> None:
    """Test full CRUD lifecycle for routing rules."""
    # Create channel
    comms_store.create_channel(
        ChannelConfig(
            id="cc-rule",
            channel_type="test",
            name="Rule",
            enabled=True,
            config_json={},
            created_at="2024",
            updated_at="2024",
        )
    )

    # Create
    rule = CommsRoutingRule(
        id="",
        name="Test Rule",
        channel_id="cc-rule",
        event_pattern="*",
        priority=10,
        enabled=True,
        config_json={"action": "reply"},
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    saved = comms_store.create_routing_rule(rule)
    assert saved.id.startswith("cr-")
    assert saved.project_id == "00000000-0000-0000-0000-000000000000"

    # Read
    fetched = comms_store.get_routing_rule(saved.id)
    assert fetched is not None
    assert fetched.name == "Test Rule"
    assert fetched.priority == 10

    # List
    rules = comms_store.list_routing_rules(channel_id="cc-rule", enabled_only=True)
    assert len(rules) == 1

    rules_empty = comms_store.list_routing_rules(enabled_only=False, channel_id="other")
    assert len(rules_empty) == 0

    # Update
    saved.priority = 20
    saved.enabled = False
    comms_store.update_routing_rule(saved)

    updated = comms_store.get_routing_rule(saved.id)
    assert updated is not None
    assert updated.priority == 20
    assert not updated.enabled

    # List enabled should be empty
    assert len(comms_store.list_routing_rules(enabled_only=True)) == 0

    # Delete
    comms_store.delete_routing_rule(saved.id)
    assert comms_store.get_routing_rule(saved.id) is None
