"""Tests for gobby-communications MCP tool registry."""

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.communications.models import ChannelConfig, CommsIdentity, CommsMessage
from gobby.mcp_proxy.tools.communications import create_communications_registry
from gobby.storage.projects import LocalProjectManager

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
    manager.send_attachment = AsyncMock()
    manager.add_channel = AsyncMock()
    manager.update_channel = AsyncMock()
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
def registry(mock_manager: MagicMock, tmp_path: Path) -> Any:
    return create_communications_registry(mock_manager, workspace_root=tmp_path)


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
        link_preview_options={"is_disabled": True},
    )

    assert res["success"] is True
    assert res["message_id"] == "msg-123"
    mock_manager.send_message.assert_called_once_with(
        channel_name="test-channel",
        content="Hello world",
        session_id="session-1",
        metadata={
            "thread_id": "thread-1",
            "content_type": "text/markdown",
            "link_preview_options": {"is_disabled": True},
        },
    )


@pytest.mark.asyncio
async def test_send_attachment_validates_path_and_returns_metadata(
    registry,
    mock_manager,
    tmp_path,
):
    image_path = tmp_path / "parity.png"
    image_path.write_bytes(b"png")
    message = MagicMock(
        id="msg-attachment",
        status="sent",
        platform_message_id="telegram-7",
        content="parity image",
        error=None,
    )
    attachment = MagicMock(
        id="attachment-1",
        message_id="msg-attachment",
        filename="parity.png",
        content_type="image/png",
        size_bytes=3,
        platform_url=None,
    )
    mock_manager.send_attachment.return_value = (message, attachment)

    result = await registry.get_tool("send_attachment")(
        channel="telegram",
        file_path=str(image_path),
        caption="parity image",
        session_id="session-1",
        metadata={"platform_destination": "chat-42"},
    )

    assert result["success"] is True
    assert result["message"]["platform_message_id"] == "telegram-7"
    assert result["attachment"]["content_type"] == "image/png"
    mock_manager.send_attachment.assert_awaited_once_with(
        channel_name="telegram",
        file_path=image_path.resolve(),
        filename=None,
        content_type="image/png",
        content="parity image",
        session_id="session-1",
        metadata={"platform_destination": "chat-42"},
    )


@pytest.mark.asyncio
async def test_send_attachment_rejects_missing_path(registry, mock_manager, tmp_path):
    result = await registry.get_tool("send_attachment")(
        channel="telegram",
        file_path=str(tmp_path / "missing.png"),
    )

    assert result["success"] is False
    assert "Invalid attachment path" in result["error"]
    mock_manager.send_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_attachment_rejects_path_outside_workspace(
    mock_manager: MagicMock,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("sensitive")
    registry = create_communications_registry(mock_manager, workspace_root=workspace)
    tool = registry.get_tool("send_attachment")
    assert tool is not None

    result = await tool(
        channel="test-channel",
        file_path=str(outside),
    )

    assert result["success"] is False
    assert result["error"] == f"Attachment path is outside the workspace: {outside}"
    mock_manager.send_attachment.assert_not_awaited()


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
    assert res["channels"][0]["project_id"] is None


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


@pytest.mark.asyncio
async def test_set_channel_project_resolves_name_and_persists_config(
    mock_manager: MagicMock,
    mock_store: MagicMock,
    temp_db: Any,
) -> None:
    project = LocalProjectManager(temp_db).create("gobby", repo_path="/tmp/gobby")
    channel = _make_channel()
    channel.config_json = {"responder": {"enabled": True}}
    mock_store.get_channel_by_name.return_value = channel
    registry = create_communications_registry(mock_manager, db=temp_db)
    handler = registry.get_tool("set_channel_project")
    assert handler is not None

    result = await handler(
        channel="test-channel",
        project="gobby",
    )

    assert result == {
        "success": True,
        "channel": "test-channel",
        "project_id": project.id,
        "project_name": "gobby",
        "project_path": "/tmp/gobby",
    }
    updated = mock_manager.update_channel.await_args.args[0]
    assert updated.config_json == {"responder": {"enabled": True, "project_id": project.id}}
    assert updated.updated_at > channel.updated_at
    assert channel.config_json == {"responder": {"enabled": True}}


@pytest.mark.asyncio
async def test_set_channel_project_rejects_unknown_project(
    mock_manager: MagicMock,
    mock_store: MagicMock,
    temp_db: Any,
) -> None:
    mock_store.get_channel_by_name.return_value = _make_channel()
    registry = create_communications_registry(mock_manager, db=temp_db)
    handler = registry.get_tool("set_channel_project")
    assert handler is not None

    result = await handler(
        channel="test-channel",
        project="missing",
    )

    assert result == {"success": False, "error": "Project 'missing' not found"}
    mock_manager.update_channel.assert_not_awaited()


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


def test_list_identities_channel_not_found(registry: Any, mock_store: MagicMock) -> None:
    mock_store.get_channel_by_name.return_value = None

    handler = registry.get_tool("list_identities")
    res = handler(channel="nope")

    assert res["success"] is False
    assert "not found" in res["error"]


def test_unlink_identity(registry: Any, mock_store: MagicMock) -> None:
    handler = registry.get_tool("unlink_identity")
    res = handler(identity_id="id-1")

    assert res["success"] is True
    mock_store.update_identity_session.assert_called_once_with("id-1", None)


@pytest.mark.asyncio
async def test_send_message_exposes_inline_keyboard_metadata(
    registry: Any,
    mock_manager: MagicMock,
) -> None:
    mock_msg = MagicMock(id="msg-123", status="sent", error=None)
    mock_manager.send_message.return_value = mock_msg
    inline_keyboard = [[{"text": "Approve", "value": "approve"}]]

    result = await registry.get_tool("send_message")(
        channel="telegram",
        content="Proceed?",
        session_id="session-1",
        inline_keyboard=inline_keyboard,
        callback_ttl_seconds=45,
    )

    assert result["success"] is True
    mock_manager.send_message.assert_awaited_once_with(
        channel_name="telegram",
        content="Proceed?",
        session_id="session-1",
        metadata={
            "inline_keyboard": inline_keyboard,
            "callback_ttl_seconds": 45,
        },
    )


def test_create_event_subscription_uses_responder_session_project(
    registry: Any,
    mock_manager: MagicMock,
) -> None:
    rule = MagicMock()
    contract = {
        "id": "sub-1",
        "name": "Agent pauses",
        "scope": {"kind": "project", "project_id": "responder-project"},
        "created_at": "2026-07-30T18:00:00-05:00",
        "updated_at": "2026-07-30T18:00:00-05:00",
    }
    mock_manager.get_session_project_id.return_value = "responder-project"
    mock_manager.create_event_subscription.return_value = rule
    mock_manager.event_subscription_to_dict.return_value = contract
    handler = registry.get_tool("create_event_subscription")

    with patch(
        "gobby.mcp_proxy.tools.communications.get_current_session_id",
        return_value="telegram-responder-session",
    ):
        result = handler(
            name="Agent pauses",
            channel="telegram",
            event_pattern="session.agent.paused",
        )

    assert result == {"success": True, "subscription": contract}
    mock_manager.get_session_project_id.assert_called_once_with("telegram-responder-session")
    mock_manager.create_event_subscription.assert_called_once_with(
        name="Agent pauses",
        channel="telegram",
        event_pattern="session.agent.paused",
        project_id="responder-project",
        global_scope=False,
        session_id=None,
        priority=0,
        enabled=True,
    )


def test_create_event_subscription_requires_caller_context_or_explicit_global(
    registry: Any,
    mock_manager: MagicMock,
) -> None:
    handler = registry.get_tool("create_event_subscription")
    with patch(
        "gobby.mcp_proxy.tools.communications.get_current_session_id",
        return_value=None,
    ):
        missing = handler(
            name="Agent pauses",
            channel="telegram",
            event_pattern="session.agent.paused",
        )

    assert missing == {"success": False, "error": "Calling session context is required"}
    mock_manager.create_event_subscription.assert_not_called()

    rule = MagicMock()
    contract = {"id": "sub-global", "scope": {"kind": "global", "project_id": None}}
    mock_manager.create_event_subscription.return_value = rule
    mock_manager.event_subscription_to_dict.return_value = contract
    explicit_global = handler(
        name="Global agent pauses",
        channel="telegram",
        event_pattern="session.agent.paused",
        global_scope=True,
    )

    assert explicit_global == {"success": True, "subscription": contract}
    assert mock_manager.create_event_subscription.call_args.kwargs["project_id"] is None
    assert mock_manager.create_event_subscription.call_args.kwargs["global_scope"] is True


def test_event_subscription_mcp_crud_uses_shared_contract(
    registry: Any,
    mock_manager: MagicMock,
) -> None:
    rule = MagicMock()
    contract = {"id": "sub-1", "scope": {"kind": "project", "project_id": "project-1"}}
    mock_manager.list_event_subscriptions.return_value = [rule]
    mock_manager.get_event_subscription.return_value = rule
    mock_manager.update_event_subscription.return_value = rule
    mock_manager.event_subscription_to_dict.return_value = contract

    listed = registry.get_tool("list_event_subscriptions")(
        channel="telegram",
        enabled=False,
        event_pattern="session.agent.paused",
    )
    fetched = registry.get_tool("get_event_subscription")("sub-1")
    updated = registry.get_tool("update_event_subscription")(
        "sub-1",
        priority=10,
        enabled=False,
    )
    deleted = registry.get_tool("delete_event_subscription")("sub-1")

    assert listed == {"success": True, "subscriptions": [contract]}
    assert fetched == {"success": True, "subscription": contract}
    assert updated == {"success": True, "subscription": contract}
    assert deleted == {"success": True, "deleted": "sub-1"}
