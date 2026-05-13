"""Tests for session bridging and identity linking in CommunicationsManager."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.communications.manager import CommunicationsManager
from gobby.communications.models import ChannelConfig, CommsIdentity, CommsMessage
from gobby.config.communications import ChannelDefaults, CommunicationsConfig


def _config(**overrides: object) -> CommunicationsConfig:
    defaults = {
        "enabled": True,
        "channel_defaults": ChannelDefaults(rate_limit_per_minute=60, burst=10),
    }
    defaults.update(overrides)
    return CommunicationsConfig(**defaults)


def _channel(
    name: str = "test-channel",
    channel_id: str = "chan-1",
    channel_type: str = "test",
) -> ChannelConfig:
    return ChannelConfig(
        id=channel_id,
        channel_type=channel_type,
        name=name,
        enabled=True,
        config_json={},
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
    )


def _identity(
    identity_id: str = "id-1",
    channel_id: str = "chan-1",
    external_user_id: str = "ext-user-1",
    external_username: str | None = "alice",
    session_id: str | None = None,
) -> CommsIdentity:
    return CommsIdentity(
        id=identity_id,
        channel_id=channel_id,
        external_user_id=external_user_id,
        external_username=external_username,
        session_id=session_id,
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
    )


def _store(channels: list[ChannelConfig] | None = None) -> MagicMock:
    store = MagicMock()
    store.list_channels.return_value = channels or []
    store.get_routing_rules.return_value = []
    store.create_message.return_value = None
    store.get_identity_by_external.return_value = None
    store.find_identities_by_username.return_value = []
    store.create_identity.side_effect = lambda identity: identity
    store.update_identity.side_effect = lambda identity: identity
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


def _session_store(session_id: str = "auto-session-1") -> MagicMock:
    session_store = MagicMock()
    session = MagicMock()
    session.id = session_id
    session_store.register.return_value = session
    return session_store


async def _make_manager(
    store: MagicMock,
    config: CommunicationsConfig | None = None,
    session_store: MagicMock | None = None,
    channels: list[ChannelConfig] | None = None,
) -> CommunicationsManager:
    """Create a manager with a started adapter."""
    cfg = config or _config()
    ss = session_store or _session_store()
    manager = CommunicationsManager(cfg, store, MagicMock(), ss)
    mock_adapter = _adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)
    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()
    return manager


# --- Identity resolution: existing identity with session ---


@pytest.mark.asyncio
async def test_resolve_identity_finds_existing():
    """_resolve_identity returns existing identity when found by external ID."""
    store = _store([_channel()])
    existing = _identity(session_id="session-abc")
    store.get_identity_by_external.return_value = existing

    manager = await _make_manager(store)
    identity = await manager._resolve_identity("chan-1", "ext-user-1", "alice")

    assert identity.session_id == "session-abc"
    assert identity.id == "id-1"
    store.get_identity_by_external.assert_called_once_with("chan-1", "ext-user-1")
    # No new identity should have been created
    store.create_identity.assert_not_called()


# --- Cross-channel identity matching by username ---


@pytest.mark.asyncio
async def test_cross_channel_identity_matching_by_username():
    """_resolve_identity finds session via cross-channel username match."""
    store = _store([_channel()])
    # No direct identity for this channel
    store.get_identity_by_external.return_value = None
    # But a matching identity exists on another channel
    other_identity = _identity(
        identity_id="id-other",
        channel_id="chan-other",
        external_user_id="ext-user-other",
        external_username="alice",
        session_id="session-from-other-channel",
    )
    store.find_identities_by_username.return_value = [other_identity]

    manager = await _make_manager(store)
    identity = await manager._resolve_identity("chan-1", "ext-user-1", "alice")

    store.find_identities_by_username.assert_called_once_with("alice")
    assert identity.session_id == "session-from-other-channel"
    # Should have created a new identity for this channel
    store.create_identity.assert_called_once()


@pytest.mark.asyncio
async def test_cross_channel_no_match_without_username():
    """Cross-channel lookup is skipped when no external_username is provided."""
    store = _store([_channel()])
    store.get_identity_by_external.return_value = None

    config = _config(auto_create_sessions=False)
    manager = await _make_manager(store, config=config)
    identity = await manager._resolve_identity("chan-1", "ext-user-1", None)

    store.find_identities_by_username.assert_not_called()
    # New identity created without session
    store.create_identity.assert_called_once()
    assert identity.session_id is None


# --- Auto-create session ---


@pytest.mark.asyncio
async def test_auto_create_session_when_no_existing():
    """_resolve_identity auto-creates a session when configured and no session found."""
    store = _store([_channel()])
    store.get_identity_by_external.return_value = None
    store.find_identities_by_username.return_value = []

    session_store = _session_store("auto-sess-123")
    config = _config(auto_create_sessions=True)
    manager = await _make_manager(store, config=config, session_store=session_store)

    identity = await manager._resolve_identity("chan-1", "ext-user-1", "alice")

    session_store.register.assert_called_once_with(
        external_id="comms:chan-1:ext-user-1",
        machine_id="comms",
        source="comms",
        project_id=None,
        title="Comms: alice",
    )
    assert identity.session_id == "auto-sess-123"


@pytest.mark.asyncio
async def test_auto_create_session_uses_user_id_when_no_username():
    """Auto-created session title falls back to external_user_id when no username."""
    store = _store([_channel()])
    store.get_identity_by_external.return_value = None
    store.find_identities_by_username.return_value = []

    session_store = _session_store("auto-sess-456")
    config = _config(auto_create_sessions=True)
    manager = await _make_manager(store, config=config, session_store=session_store)

    await manager._resolve_identity("chan-1", "ext-user-1", None)

    call_kwargs = session_store.register.call_args[1]
    assert call_kwargs["title"] == "Comms: ext-user-1"


@pytest.mark.asyncio
async def test_no_auto_create_session_when_disabled():
    """No session is created when auto_create_sessions is False."""
    store = _store([_channel()])
    store.get_identity_by_external.return_value = None
    store.find_identities_by_username.return_value = []

    session_store = _session_store()
    config = _config(auto_create_sessions=False)
    manager = await _make_manager(store, config=config, session_store=session_store)

    identity = await manager._resolve_identity("chan-1", "ext-user-1", "alice")

    session_store.register.assert_not_called()
    assert identity.session_id is None


# --- Identity update on resolve ---


@pytest.mark.asyncio
async def test_resolve_updates_session_on_existing_identity():
    """Existing identity gets session_id updated when cross-channel match provides one."""
    store = _store([_channel()])
    existing = _identity(session_id=None)
    store.get_identity_by_external.return_value = existing
    # Cross-channel match provides session
    other_identity = _identity(
        identity_id="id-other",
        channel_id="chan-other",
        session_id="session-from-cross",
    )
    store.find_identities_by_username.return_value = [other_identity]

    manager = await _make_manager(store)
    identity = await manager._resolve_identity("chan-1", "ext-user-1", "alice")

    assert identity.session_id == "session-from-cross"
    store.update_identity.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_updates_username_on_existing_identity():
    """Existing identity gets username updated when new username differs."""
    store = _store([_channel()])
    existing = _identity(session_id="session-abc", external_username="old_name")
    store.get_identity_by_external.return_value = existing

    manager = await _make_manager(store)
    identity = await manager._resolve_identity("chan-1", "ext-user-1", "new_name")

    assert identity.external_username == "new_name"
    store.update_identity.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_no_update_when_nothing_changed():
    """Existing identity is not updated when session and username match."""
    store = _store([_channel()])
    existing = _identity(session_id="session-abc", external_username="alice")
    store.get_identity_by_external.return_value = existing

    manager = await _make_manager(store)
    identity = await manager._resolve_identity("chan-1", "ext-user-1", "alice")

    assert identity.session_id == "session-abc"
    store.update_identity.assert_not_called()


# --- Bridge identity ---


def test_bridge_identity_links_session():
    """_bridge_identity links an existing identity to a session."""
    store = _store()
    existing = _identity(session_id=None)
    store.get_identity.return_value = existing

    manager = CommunicationsManager(_config(), store, MagicMock(), MagicMock())
    manager._bridge_identity("id-1", "session-xyz")

    assert existing.session_id == "session-xyz"
    store.update_identity.assert_called_once_with(existing)


def test_bridge_identity_noop_when_not_found():
    """_bridge_identity is a no-op if identity doesn't exist."""
    store = _store()
    store.get_identity.return_value = None

    manager = CommunicationsManager(_config(), store, MagicMock(), MagicMock())
    manager._bridge_identity("nonexistent-id", "session-xyz")

    store.update_identity.assert_not_called()
    assert store.update_identity.call_count == 0
    assert not store.update_identity.called


# --- find_cross_channel_identity ---


def test_find_cross_channel_identity_returns_session():
    """_find_cross_channel_identity returns session_id from matching identity."""
    store = _store()
    matching = _identity(session_id="session-match")
    store.find_identities_by_username.return_value = [matching]

    manager = CommunicationsManager(_config(), store, MagicMock(), MagicMock())
    result = manager._find_cross_channel_identity("alice")

    assert result == "session-match"
    store.find_identities_by_username.assert_called_once_with("alice")


def test_find_cross_channel_identity_skips_no_session():
    """_find_cross_channel_identity skips identities without a session."""
    store = _store()
    no_session = _identity(session_id=None)
    with_session = _identity(identity_id="id-2", session_id="session-2")
    store.find_identities_by_username.return_value = [no_session, with_session]

    manager = CommunicationsManager(_config(), store, MagicMock(), MagicMock())
    result = manager._find_cross_channel_identity("alice")

    assert result == "session-2"


def test_find_cross_channel_identity_returns_none_when_empty():
    """_find_cross_channel_identity returns None when no identities found."""
    store = _store()
    store.find_identities_by_username.return_value = []

    manager = CommunicationsManager(_config(), store, MagicMock(), MagicMock())
    result = manager._find_cross_channel_identity("alice")

    assert result is None


# --- End-to-end: inbound message triggers identity resolution ---


@pytest.mark.asyncio
async def test_inbound_message_resolves_identity_and_sets_session():
    """handle_inbound_messages resolves identity and populates session_id on message."""
    channel = _channel()
    store = _store([channel])
    existing = _identity(session_id="session-abc")
    store.get_identity_by_external.return_value = existing

    manager = await _make_manager(store)

    inbound = CommsMessage(
        id="msg-1",
        channel_id="chan-1",
        direction="inbound",
        content="Hello",
        created_at="2024-01-01T00:00:00",
        identity_id="ext-user-1",
        metadata_json={"external_username": "alice"},
    )

    stored = await manager.handle_inbound_messages("test-channel", [inbound])

    assert len(stored) == 1
    assert stored[0].session_id == "session-abc"
    assert stored[0].identity_id == "id-1"
