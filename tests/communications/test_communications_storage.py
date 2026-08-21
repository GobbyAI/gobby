"""Tests for local communications store."""

import uuid
from datetime import UTC, datetime

import pytest

from gobby.communications.identities import IdentityManager
from gobby.communications.models import (
    ChannelConfig,
    CommsIdentity,
    CommsMessage,
    CommsRoutingRule,
)
from gobby.config.communications import CommunicationsConfig
from gobby.storage.communications import LocalCommunicationsStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.integration

_TS = datetime(2024, 1, 1, tzinfo=UTC)
_TS_PLUS_ONE_SECOND = datetime(2024, 1, 1, 0, 0, 1, tzinfo=UTC)


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
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        updated_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    saved = comms_store.create_channel(channel)
    assert str(uuid.UUID(saved.id)) == saved.id
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
        id="cccccccc-1111-4ccc-8ccc-cccccccc0001",
        channel_type="test",
        name="Test",
        enabled=True,
        config_json={},
        created_at=_TS,
        updated_at=_TS,
    )
    comms_store.create_channel(channel)

    # Create
    identity = CommsIdentity(
        id="",
        channel_id="cccccccc-1111-4ccc-8ccc-cccccccc0001",
        external_user_id="user_123",
        external_username="testuser",
        session_id=None,
        project_id=None,  # Should use store's project_id
        metadata_json={"role": "admin"},
        created_at=_TS,
        updated_at=_TS,
    )
    saved = comms_store.create_identity(identity)
    assert str(uuid.UUID(saved.id)) == saved.id
    assert saved.project_id == "00000000-0000-0000-0000-000000000000"

    # Read
    fetched = comms_store.get_identity(saved.id)
    assert fetched is not None
    assert fetched.external_username == "testuser"
    assert fetched.metadata_json == {"role": "admin"}

    fetched_ext = comms_store.get_identity_by_external(
        "cccccccc-1111-4ccc-8ccc-cccccccc0001", "user_123"
    )
    assert fetched_ext is not None
    assert fetched_ext.id == saved.id

    # List
    identities = comms_store.list_identities(channel_id="cccccccc-1111-4ccc-8ccc-cccccccc0001")
    assert len(identities) == 1

    identities_none = comms_store.list_identities(channel_id="00000000-0000-0000-0000-0000000000ff")
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


def test_resolve_identity_creates_real_store_identity_with_timestamps(
    temp_db: HubDatabase,
) -> None:
    """Resolve a new external user through the real store with generated timestamps."""
    comms_store = LocalCommunicationsStore(
        temp_db, project_id="00000000-0000-0000-0000-000000000000"
    )
    channel = ChannelConfig(
        id="cccccccc-1111-4ccc-8ccc-cccccccc0002",
        channel_type="test",
        name="Resolve Test",
        enabled=True,
        config_json={},
        created_at=_TS,
        updated_at=_TS,
    )
    comms_store.create_channel(channel)

    identities = IdentityManager(
        comms_store,
        SessionManager(temp_db),
        CommunicationsConfig(auto_create_sessions=True),
    )
    identity = identities.resolve_identity(
        channel_id="cccccccc-1111-4ccc-8ccc-cccccccc0002",
        external_user_id="new-user",
        external_username="newuser",
        metadata={"source": "telegram"},
        project_id="00000000-0000-0000-0000-000000000000",
    )

    assert str(uuid.UUID(identity.id)) == identity.id
    assert identity.session_id
    assert identity.created_at
    assert identity.updated_at
    assert identity.metadata_json == {"source": "telegram"}

    stored = comms_store.get_identity_by_external(
        "cccccccc-1111-4ccc-8ccc-cccccccc0002", "new-user"
    )
    assert stored is not None
    assert stored.id == identity.id
    assert stored.created_at == identity.created_at
    assert stored.updated_at == identity.updated_at


def test_message_crud(comms_store: LocalCommunicationsStore) -> None:
    """Test full CRUD lifecycle for messages."""
    # Create channel & identity
    comms_store.create_channel(
        ChannelConfig(
            id="cccccccc-1111-4ccc-8ccc-cccccccc0003",
            channel_type="test",
            name="Msg",
            enabled=True,
            config_json={},
            created_at=_TS,
            updated_at=_TS,
        )
    )
    comms_store.create_identity(
        CommsIdentity(
            id="cccccccc-2222-4ccc-8ccc-cccccccc0001",
            channel_id="cccccccc-1111-4ccc-8ccc-cccccccc0003",
            external_user_id="u1",
            created_at=_TS,
            updated_at=_TS,
        )
    )

    # Create
    message = CommsMessage(
        id="",
        channel_id="cccccccc-1111-4ccc-8ccc-cccccccc0003",
        identity_id="cccccccc-2222-4ccc-8ccc-cccccccc0001",
        direction="inbound",
        content="Hello world",
        content_type="text",
        platform_message_id="msg_1",
        session_id=None,
        status="sent",
        metadata_json={"tokens": 10},
        created_at=_TS,
    )
    saved = comms_store.create_message(message)
    assert str(uuid.UUID(saved.id)) == saved.id

    # Read
    fetched = comms_store.get_message(saved.id)
    assert fetched is not None
    assert fetched.content == "Hello world"
    assert fetched.direction == "inbound"

    # List (Filter and sort)
    messages = comms_store.list_messages(
        channel_id="cccccccc-1111-4ccc-8ccc-cccccccc0003", direction="inbound"
    )
    assert len(messages) == 1
    assert messages[0].id == saved.id

    messages_empty = comms_store.list_messages(session_id="00000000-0000-0000-0000-0000000000ff")
    assert len(messages_empty) == 0

    # Update status
    comms_store.update_message_status(saved.id, "delivered", "no error")
    updated = comms_store.get_message(saved.id)
    assert updated is not None
    assert updated.status == "delivered"
    assert updated.error == "no error"

    comms_store.update_message_delivery(
        saved.id,
        "sent",
        None,
        "msg_root",
        {
            "tokens": 10,
            "platform_message_ids": ["msg_root", "msg_tail"],
            "platform_destination": "chat-a",
        },
    )
    alias = comms_store.get_message_by_platform_id(
        "Msg",
        "msg_tail",
        platform_destination="chat-a",
    )
    assert alias is not None
    assert alias.id == saved.id
    assert alias.platform_message_id == "msg_root"
    assert alias.metadata_json["platform_message_ids"] == ["msg_root", "msg_tail"]
    assert (
        comms_store.get_message_by_platform_id(
            "Msg",
            "msg_tail",
            platform_destination="chat-b",
        )
        is None
    )

    comms_store.update_message_content(saved.id, "Edited response")
    edited = comms_store.get_message(saved.id)
    assert edited is not None
    assert edited.content == "Edited response"


def test_create_message_deduplicates_channel_platform_message_id(
    comms_store: LocalCommunicationsStore,
) -> None:
    """Webhook retries should not create duplicate platform-message rows."""
    comms_store.create_channel(
        ChannelConfig(
            id="cccccccc-1111-4ccc-8ccc-cccccccc0004",
            channel_type="test",
            name="Dedup",
            enabled=True,
            config_json={},
            created_at=_TS,
            updated_at=_TS,
        )
    )

    first = comms_store.create_message(
        CommsMessage(
            id="cccccccc-3333-4ccc-8ccc-cccccccc0001",
            channel_id="cccccccc-1111-4ccc-8ccc-cccccccc0004",
            direction="inbound",
            content="first",
            platform_message_id="platform-1",
            created_at=_TS,
        )
    )
    duplicate = comms_store.create_message(
        CommsMessage(
            id="cccccccc-3333-4ccc-8ccc-cccccccc0002",
            channel_id="cccccccc-1111-4ccc-8ccc-cccccccc0004",
            direction="inbound",
            content="second",
            platform_message_id="platform-1",
            created_at=_TS_PLUS_ONE_SECOND,
        )
    )

    messages = comms_store.list_messages(channel_id="cccccccc-1111-4ccc-8ccc-cccccccc0004")

    assert duplicate.id == first.id
    assert len(messages) == 1
    assert messages[0].content == "first"


def test_routing_rule_crud(comms_store: LocalCommunicationsStore) -> None:
    """Test full CRUD lifecycle and exact administrative filters for routing rules."""
    project_id = "00000000-0000-0000-0000-000000000000"
    channel_id = "cccccccc-1111-4ccc-8ccc-cccccccc0005"
    comms_store.create_channel(
        ChannelConfig(
            id=channel_id,
            channel_type="test",
            name="Rule",
            enabled=True,
            config_json={},
            created_at=_TS,
            updated_at=_TS,
        )
    )

    rule = CommsRoutingRule(
        id="dddddddd-1111-4ddd-8ddd-dddddddd0001",
        name="Test Rule",
        channel_id=channel_id,
        event_pattern="task.*",
        project_id=project_id,
        priority=10,
        enabled=True,
        config_json={},
        created_at=_TS,
        updated_at=_TS,
    )
    saved = comms_store.create_routing_rule(rule)
    assert saved.id == rule.id
    assert saved.project_id == project_id

    comms_store.create_routing_rule(
        CommsRoutingRule(
            id="dddddddd-1111-4ddd-8ddd-dddddddd0002",
            name="Disabled Rule",
            channel_id=channel_id,
            event_pattern="session.agent.paused",
            project_id=project_id,
            priority=20,
            enabled=False,
            config_json={},
            created_at=datetime(2024, 1, 1, 0, 0, 1, tzinfo=UTC),
            updated_at=datetime(2024, 1, 1, 0, 0, 1, tzinfo=UTC),
        )
    )
    comms_store.create_routing_rule(
        CommsRoutingRule(
            id="dddddddd-1111-4ddd-8ddd-dddddddd0003",
            name="Global Rule",
            channel_id=channel_id,
            event_pattern="*",
            project_id=None,
            priority=0,
            enabled=True,
            config_json={},
            created_at=datetime(2024, 1, 1, 0, 0, 2, tzinfo=UTC),
            updated_at=datetime(2024, 1, 1, 0, 0, 2, tzinfo=UTC),
        )
    )

    fetched = comms_store.get_routing_rule(saved.id)
    assert fetched is not None
    assert fetched.name == "Test Rule"
    assert fetched.priority == 10

    assert len(comms_store.list_routing_rules()) == 3
    assert len(comms_store.list_routing_rules(channel_id=channel_id)) == 3
    assert len(comms_store.list_routing_rules(project_id=project_id)) == 2
    assert len(comms_store.list_routing_rules(global_scope=True)) == 1
    assert len(comms_store.list_routing_rules(global_scope=False)) == 2
    assert len(comms_store.list_routing_rules(enabled=True)) == 2
    assert len(comms_store.list_routing_rules(enabled=False)) == 1
    assert len(comms_store.list_routing_rules(event_pattern="task.*")) == 1
    assert (
        len(comms_store.list_routing_rules(channel_id="00000000-0000-0000-0000-0000000000ff")) == 0
    )

    saved.priority = 20
    saved.enabled = False
    comms_store.update_routing_rule(saved)

    updated = comms_store.get_routing_rule(saved.id)
    assert updated is not None
    assert updated.priority == 20
    assert not updated.enabled

    comms_store.delete_routing_rule(saved.id)
    assert comms_store.get_routing_rule(saved.id) is None
