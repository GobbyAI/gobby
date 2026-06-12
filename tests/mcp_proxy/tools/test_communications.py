"""Tests for gobby-communications MCP tool registry."""

from dataclasses import asdict
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.communications.models import ChannelConfig, CommsIdentity, CommsMessage
from gobby.mcp_proxy.tools.communications import create_communications_registry

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_store():
    store = MagicMock()
    return store


@pytest.fixture
def mock_manager(mock_store):
    manager = MagicMock()
    manager._store = mock_store
    manager.send_message = AsyncMock()
    manager.add_channel = AsyncMock()
    manager.remove_channel = AsyncMock()
    manager.get_channel_status = MagicMock(return_value={"connected": True})
    manager.channel_to_dict.side_effect = lambda channel: {
        **asdict(channel),
        "active": True,
        "init_error": None,
    }
    # Public delegation methods (mirror CommunicationsManager API)
    manager.list_channels = mock_store.list_channels
    manager.get_channel_by_name = mock_store.get_channel_by_name
    manager.list_messages = mock_store.list_messages
    manager.get_identity_by_external = mock_store.get_identity_by_external
    manager.list_identities = mock_store.list_identities
    manager.update_identity_session = mock_store.update_identity_session
    return manager


@pytest.fixture
def registry(mock_manager):
    return create_communications_registry(mock_manager)


@pytest.mark.asyncio
async def test_send_message(registry, mock_manager):
    mock_msg = MagicMock()
    mock_msg.id = "msg-123"
    mock_msg.status = "sent"
    mock_msg.error = None
    mock_manager.send_message.return_value = mock_msg

    handler = registry.get_tool("send_message")

    res = await handler(
        channel="test-channel",
        content="Hello world",
        session_id="session-1",
        thread_id="thread-1",
        content_type="text/markdown",
    )

    assert res["success"] is True
    assert res["message_id"] == "msg-123"
    mock_manager.send_message.assert_called_once_with(
        channel_name="test-channel",
        content="Hello world",
        session_id="session-1",
        metadata={"thread_id": "thread-1", "content_type": "text/markdown"},
    )


async def test_send_message_reports_failed_status(registry, mock_manager):
    mock_msg = MagicMock()
    mock_msg.id = "msg-123"
    mock_msg.status = "failed"
    mock_msg.error = "network error"
    mock_manager.send_message.return_value = mock_msg

    handler = registry.get_tool("send_message")

    res = await handler(channel="test-channel", content="Hello world")

    assert res == {"success": False, "message_id": "msg-123", "error": "network error"}


def test_list_channels(registry, mock_store, mock_manager):
    channel = ChannelConfig(
        id="ch-1",
        channel_type="slack",
        name="test-channel",
        enabled=True,
        config_json={},
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
    )
    mock_store.list_channels.return_value = [channel]

    handler = registry.get_tool("list_channels")

    res = handler()
    assert res["success"] is True
    assert len(res["channels"]) == 1
    assert res["channels"][0]["name"] == "test-channel"


def test_get_messages(registry, mock_store):
    channel = ChannelConfig(
        id="ch-1",
        channel_type="slack",
        name="test-channel",
        enabled=True,
        config_json={},
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
    )
    mock_store.get_channel_by_name.return_value = channel

    msg = CommsMessage(
        id="msg-1",
        channel_id="ch-1",
        direction="inbound",
        content="Hello",
        created_at=datetime.now().isoformat(),
        session_id="session-1",
    )
    mock_store.list_messages.return_value = [msg]

    handler = registry.get_tool("get_messages")

    res = handler(channel="test-channel")
    assert res["success"] is True
    assert len(res["messages"]) == 1
    assert res["messages"][0]["content"] == "Hello"


@pytest.mark.asyncio
async def test_add_channel(registry, mock_manager):
    channel = ChannelConfig(
        id="ch-new",
        channel_type="slack",
        name="new-channel",
        enabled=True,
        config_json={},
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
    )
    mock_manager.add_channel.return_value = channel

    handler = registry.get_tool("add_channel")

    res = await handler(channel_type="slack", name="new-channel", config={})
    assert res["success"] is True
    assert res["channel_id"] == "ch-new"
    assert res["active"] is True
    assert res["init_error"] is None


async def test_add_channel_reports_init_error(registry, mock_manager):
    channel = ChannelConfig(
        id="ch-new",
        channel_type="slack",
        name="new-channel",
        enabled=True,
        config_json={},
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
    )
    mock_manager.add_channel.return_value = channel
    mock_manager.channel_to_dict.return_value = {
        **asdict(channel),
        "active": False,
        "init_error": "bad token",
    }
    mock_manager.channel_to_dict.side_effect = None

    handler = registry.get_tool("add_channel")

    res = await handler(channel_type="slack", name="new-channel", config={})
    assert res["success"] is False
    assert res["channel_id"] == "ch-new"
    assert res["active"] is False
    assert res["init_error"] == "bad token"
    assert res["channel"]["init_error"] == "bad token"


@pytest.mark.asyncio
async def test_remove_channel(registry, mock_manager):
    handler = registry.get_tool("remove_channel")

    res = await handler(name="old-channel")
    assert res["success"] is True
    mock_manager.remove_channel.assert_called_once_with(name="old-channel")


def _make_channel(id: str = "ch-1", name: str = "test-channel") -> ChannelConfig:
    return ChannelConfig(
        id=id,
        channel_type="slack",
        name=name,
        enabled=True,
        config_json={},
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
    )


def _make_identity(
    id: str = "id-1",
    channel_id: str = "ch-1",
    external_user_id: str = "ext-1",
    external_username: str = "alice",
    session_id: str | None = "session-1",
) -> CommsIdentity:
    return CommsIdentity(
        id=id,
        channel_id=channel_id,
        external_user_id=external_user_id,
        external_username=external_username,
        session_id=session_id,
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
    )


def test_link_identity_success(registry, mock_store):
    mock_store.get_channel_by_name.return_value = _make_channel()
    mock_store.get_identity_by_external.return_value = _make_identity()

    handler = registry.get_tool("link_identity")
    res = handler(channel="test-channel", external_user_id="ext-1", session_id="session-99")

    assert res["success"] is True
    assert res["identity_id"] == "id-1"
    mock_store.update_identity_session.assert_called_once_with("id-1", "session-99")


def test_link_identity_channel_not_found(registry, mock_store):
    mock_store.get_channel_by_name.return_value = None

    handler = registry.get_tool("link_identity")
    res = handler(channel="nope", external_user_id="ext-1", session_id="session-99")

    assert res["success"] is False
    assert "not found" in res["error"]


def test_link_identity_identity_not_found(registry, mock_store):
    mock_store.get_channel_by_name.return_value = _make_channel()
    mock_store.get_identity_by_external.return_value = None

    handler = registry.get_tool("link_identity")
    res = handler(channel="test-channel", external_user_id="ext-missing", session_id="session-99")

    assert res["success"] is False
    assert "not found" in res["error"]


def test_list_identities_no_filters(registry, mock_store):
    identities = [_make_identity(), _make_identity(id="id-2", external_user_id="ext-2")]
    mock_store.list_identities.return_value = identities

    handler = registry.get_tool("list_identities")
    res = handler()

    assert res["success"] is True
    assert len(res["identities"]) == 2
    mock_store.list_identities.assert_called_once_with(channel_id=None)


def test_list_identities_filter_by_channel(registry, mock_store):
    mock_store.get_channel_by_name.return_value = _make_channel()
    mock_store.list_identities.return_value = [_make_identity()]

    handler = registry.get_tool("list_identities")
    res = handler(channel="test-channel")

    assert res["success"] is True
    mock_store.list_identities.assert_called_once_with(channel_id="ch-1")


def test_list_identities_filter_by_session(registry, mock_store):
    identities = [
        _make_identity(id="id-1", session_id="session-1"),
        _make_identity(id="id-2", session_id="session-2"),
    ]
    mock_store.list_identities.return_value = identities

    handler = registry.get_tool("list_identities")
    res = handler(session_id="session-1")

    assert res["success"] is True
    assert len(res["identities"]) == 1
    assert res["identities"][0]["id"] == "id-1"


def test_list_identities_channel_not_found(registry, mock_store):
    mock_store.get_channel_by_name.return_value = None

    handler = registry.get_tool("list_identities")
    res = handler(channel="nope")

    assert res["success"] is False
    assert "not found" in res["error"]


def test_unlink_identity(registry, mock_store):
    handler = registry.get_tool("unlink_identity")
    res = handler(identity_id="id-1")

    assert res["success"] is True
    mock_store.update_identity_session.assert_called_once_with("id-1", None)
