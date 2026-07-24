"""Tests for inbound communications identity and session resolution."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.communications.identities import IdentityManager
from gobby.communications.inbound import InboundCommunications
from gobby.communications.models import ChannelConfig, CommsIdentity, CommsMessage
from gobby.config.communications import CommunicationsConfig
from gobby.utils.datetime import utc_now


def _identity(
    external_user_id: str,
    *,
    identity_id: str,
    session_id: str | None = None,
) -> CommsIdentity:
    now = utc_now()
    return CommsIdentity(
        id=identity_id,
        channel_id="channel-1",
        external_user_id=external_user_id,
        session_id=session_id,
        created_at=now,
        updated_at=now,
    )


def _manager(
    *,
    store: MagicMock,
    session_store: MagicMock,
) -> IdentityManager:
    return IdentityManager(
        store=store,
        session_store=session_store,
        config=CommunicationsConfig(enabled=True),
    )


def test_dm_resolution_links_sender_scoped_session() -> None:
    store = MagicMock()
    store.get_identity_by_external.return_value = None
    store.create_identity.side_effect = lambda identity: identity
    session_store = MagicMock()
    session_store.register.return_value.id = "dm-session"
    manager = _manager(store=store, session_store=session_store)

    resolution = manager.resolve_inbound_identity(
        "channel-1",
        "user-1",
        "alice",
    )

    assert resolution.session_id == "dm-session"
    assert resolution.identity.session_id == "dm-session"
    session_store.register.assert_called_once_with(
        external_id="comms:channel-1:user-1",
        machine_id="comms",
        source="comms",
        project_id=None,
        title="Comms: alice",
    )


def test_group_resolution_shares_chat_session_across_sender_identities() -> None:
    store = MagicMock()
    store.get_identity_by_external.return_value = None
    store.create_identity.side_effect = lambda identity: identity
    session_store = MagicMock()
    session_store.register.return_value.id = "group-session"
    manager = _manager(store=store, session_store=session_store)

    first = manager.resolve_inbound_identity(
        "channel-1",
        "user-1",
        "alice",
        group_chat_id="-10042",
    )
    second = manager.resolve_inbound_identity(
        "channel-1",
        "user-2",
        "bob",
        group_chat_id="-10042",
    )

    assert first.session_id == second.session_id == "group-session"
    assert first.identity.external_user_id == "user-1"
    assert second.identity.external_user_id == "user-2"
    assert first.identity.session_id is None
    assert second.identity.session_id is None
    assert [call.kwargs["external_id"] for call in session_store.register.call_args_list] == [
        "comms:channel-1:group:-10042",
        "comms:channel-1:group:-10042",
    ]


def test_group_resolution_preserves_existing_dm_session_link() -> None:
    dm_identity = _identity("user-1", identity_id="identity-1", session_id="dm-session")
    store = MagicMock()
    store.get_identity_by_external.return_value = dm_identity
    session_store = MagicMock()
    session_store.register.return_value.id = "group-session"
    manager = _manager(store=store, session_store=session_store)

    group = manager.resolve_inbound_identity(
        "channel-1",
        "user-1",
        "alice",
        group_chat_id="-10042",
    )
    direct = manager.resolve_inbound_identity(
        "channel-1",
        "user-1",
        "alice",
    )

    assert group.session_id == "group-session"
    assert direct.session_id == "dm-session"
    assert dm_identity.session_id == "dm-session"


@pytest.mark.asyncio
async def test_inbound_group_metadata_selects_chat_scoped_session() -> None:
    channel = ChannelConfig(
        id="channel-1",
        channel_type="telegram",
        name="telegram",
        enabled=True,
        config_json={},
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    store = MagicMock()
    store.get_message_by_platform_id.return_value = None
    store.get_identity_by_external.return_value = None

    def create_identity(identity: CommsIdentity) -> CommsIdentity:
        identity.id = "identity-1"
        return identity

    store.create_identity.side_effect = create_identity
    store.create_message.side_effect = lambda message: message
    session_store = MagicMock()
    session_store.register.return_value.id = "group-session"

    manager = MagicMock()
    manager._channel_by_name = {"telegram": channel}
    manager._adapters = {}
    manager._store = store
    manager._identity_manager = _manager(store=store, session_store=session_store)
    manager.event_callback = None
    manager.reaction_handler = None

    message = CommsMessage(
        id="message-1",
        channel_id="",
        direction="inbound",
        content="Hello group",
        identity_id="user-1",
        created_at=utc_now(),
        metadata_json={
            "conversation_type": "supergroup",
            "chat_id": "-10042",
            "external_username": "alice",
        },
    )

    handled = await InboundCommunications(manager).handle_messages("telegram", [message])

    assert handled[0].session_id == "group-session"
    assert handled[0].identity_id == "identity-1"
    session_store.register.assert_called_once_with(
        external_id="comms:channel-1:group:-10042",
        machine_id="comms",
        source="comms",
        project_id=None,
        title="Comms group: -10042",
    )
