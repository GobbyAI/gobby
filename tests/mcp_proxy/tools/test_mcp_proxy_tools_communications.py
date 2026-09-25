"""Tests for gobby-communications MCP tool registry."""

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.communications.models import ChannelConfig, CommsIdentity, CommsMessage
from gobby.mcp_proxy.tools.communications import create_communications_registry

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_store() -> MagicMock:
    store = MagicMock()
    return store


@pytest.fixture
def mock_manager(mock_store: MagicMock) -> MagicMock:
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
async def test_send_message(registry: Any, mock_manager: MagicMock) -> None:
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
async def test_send_message_inherits_caller_only_when_omitted(
    registry: Any, mock_manager: MagicMock
) -> None:
    mock_manager.send_message.return_value = MagicMock(status="sent", id="outbound", error=None)
    with patch(
        "gobby.mcp_proxy.tools.communications.get_current_session_id", return_value="caller"
    ):
        await registry.get_tool("send_message")(channel="telegram", content="hello")
        await registry.get_tool("send_message")(
            channel="telegram", content="explicit", session_id="other"
        )
    assert mock_manager.send_message.await_args_list[0].kwargs["session_id"] == "caller"
    assert mock_manager.send_message.await_args_list[1].kwargs["session_id"] == "other"

    with patch("gobby.mcp_proxy.tools.communications.get_current_session_id", return_value=None):
        await registry.get_tool("send_message")(channel="telegram", content="anonymous")
    assert mock_manager.send_message.await_args.kwargs["session_id"] is None


def test_attach_and_detach_use_caller_session(registry: Any, mock_manager: MagicMock) -> None:
    with patch(
        "gobby.mcp_proxy.tools.communications.get_current_session_id", return_value="caller"
    ):
        attached = registry.get_tool("attach_conversation")(
            channel="telegram", conversation_id="dm:42"
        )
        detached = registry.get_tool("detach_conversation")(
            channel="telegram", conversation_id="dm:42"
        )
    assert attached["success"] is True
    assert detached["success"] is True
    mock_manager.attach_conversation.assert_called_once_with("telegram", "dm:42", "caller")
    mock_manager.detach_conversation.assert_called_once_with("telegram", "dm:42", "caller")


@pytest.mark.asyncio
async def test_send_attachment_validates_path_and_returns_metadata(
    registry: Any,
    mock_manager: MagicMock,
    tmp_path: Path,
) -> None:
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
async def test_send_attachment_rejects_missing_path(
    registry: Any, mock_manager: MagicMock, tmp_path: Path
) -> None:
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


async def test_send_message_reports_failed_status(registry: Any, mock_manager: MagicMock) -> None:
    mock_msg = MagicMock()
    mock_msg.id = "msg-123"
    mock_msg.status = "failed"
    mock_msg.error = "network error"
    mock_manager.send_message.return_value = mock_msg

    handler = registry.get_tool("send_message")

    res = await handler(channel="test-channel", content="Hello world")

    assert res == {"success": False, "message_id": "msg-123", "error": "network error"}


def test_list_channels(registry: Any, mock_store: MagicMock, mock_manager: MagicMock) -> None:
    channel = ChannelConfig(
        id="ch-1",
        channel_type="slack",
        name="test-channel",
        enabled=True,
        config_json={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    mock_store.list_channels.return_value = [channel]

    handler = registry.get_tool("list_channels")

    res = handler()
    assert res["success"] is True
    assert len(res["channels"]) == 1
    assert res["channels"][0]["name"] == "test-channel"
    assert res["channels"][0]["project_id"] is None


def test_get_messages(registry: Any, mock_store: MagicMock) -> None:
    channel = ChannelConfig(
        id="ch-1",
        channel_type="slack",
        name="test-channel",
        enabled=True,
        config_json={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    mock_store.get_channel_by_name.return_value = channel

    msg = CommsMessage(
        id="msg-1",
        channel_id="ch-1",
        direction="inbound",
        content="Hello",
        created_at=datetime.now(UTC),
        session_id="session-1",
    )
    mock_store.list_messages.return_value = [msg]

    handler = registry.get_tool("get_messages")

    res = handler(channel="test-channel")
    assert res["success"] is True
    assert len(res["messages"]) == 1
    assert res["messages"][0]["content"] == "Hello"


@pytest.mark.asyncio
async def test_add_channel(registry: Any, mock_manager: MagicMock) -> None:
    channel = ChannelConfig(
        id="ch-new",
        channel_type="slack",
        name="new-channel",
        enabled=True,
        config_json={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    mock_manager.add_channel.return_value = channel

    handler = registry.get_tool("add_channel")

    res = await handler(channel_type="slack", name="new-channel", config={})
    assert res["success"] is True
    assert res["channel_id"] == "ch-new"
    assert res["active"] is True
    assert res["init_error"] is None


async def test_add_channel_reports_init_error(registry: Any, mock_manager: MagicMock) -> None:
    channel = ChannelConfig(
        id="ch-new",
        channel_type="slack",
        name="new-channel",
        enabled=True,
        config_json={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
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
async def test_remove_channel(registry: Any, mock_manager: MagicMock) -> None:
    handler = registry.get_tool("remove_channel")

    res = await handler(name="old-channel")
    assert res["success"] is True
    mock_manager.remove_channel.assert_called_once_with(name="old-channel")


@pytest.mark.asyncio
async def test_set_channel_project_resolves_name_and_persists_config(
    mock_manager: MagicMock,
    mock_store: MagicMock,
    temp_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.fixtures.isolated_checkout import install_isolated_checkout_project

    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "gobby", name="gobby", monkeypatch=monkeypatch
    )
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
        "project_id": isolated.project.id,
        "project_name": "gobby",
        "project_path": isolated.root_path,
    }
    updated = mock_manager.update_channel.await_args.args[0]
    assert updated.config_json == {
        "responder": {"enabled": True, "project_id": isolated.project.id}
    }
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
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def test_link_identity_success(registry: Any, mock_store: MagicMock) -> None:
    mock_store.get_channel_by_name.return_value = _make_channel()
    mock_store.get_identity_by_external.return_value = _make_identity()

    handler = registry.get_tool("link_identity")
    res = handler(channel="test-channel", external_user_id="ext-1", session_id="session-99")

    assert res["success"] is True
    assert res["identity_id"] == "id-1"
    mock_store.update_identity_session.assert_called_once_with("id-1", "session-99")


def test_link_identity_channel_not_found(registry: Any, mock_store: MagicMock) -> None:
    mock_store.get_channel_by_name.return_value = None

    handler = registry.get_tool("link_identity")
    res = handler(channel="nope", external_user_id="ext-1", session_id="session-99")

    assert res["success"] is False
    assert "not found" in res["error"]


def test_link_identity_identity_not_found(registry: Any, mock_store: MagicMock) -> None:
    mock_store.get_channel_by_name.return_value = _make_channel()
    mock_store.get_identity_by_external.return_value = None

    handler = registry.get_tool("link_identity")
    res = handler(channel="test-channel", external_user_id="ext-missing", session_id="session-99")

    assert res["success"] is False
    assert "not found" in res["error"]


def test_list_identities_no_filters(registry: Any, mock_store: MagicMock) -> None:
    identities = [_make_identity(), _make_identity(id="id-2", external_user_id="ext-2")]
    mock_store.list_identities.return_value = identities

    handler = registry.get_tool("list_identities")
    res = handler()

    assert res["success"] is True
    assert len(res["identities"]) == 2
    mock_store.list_identities.assert_called_once_with(channel_id=None)


def test_list_identities_filter_by_channel(registry: Any, mock_store: MagicMock) -> None:
    mock_store.get_channel_by_name.return_value = _make_channel()
    mock_store.list_identities.return_value = [_make_identity()]

    handler = registry.get_tool("list_identities")
    res = handler(channel="test-channel")

    assert res["success"] is True
    mock_store.list_identities.assert_called_once_with(channel_id="ch-1")


def test_list_identities_filter_by_session(registry: Any, mock_store: MagicMock) -> None:
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


@pytest.mark.asyncio
async def test_send_message_stores_the_current_project_on_a_keyboard(
    mock_manager: MagicMock,
    tmp_path: Path,
) -> None:
    """Josh addresses the button with #N. The tap can resolve it only with this project."""
    mock_msg = MagicMock(id="msg-123", status="sent", error=None)
    mock_manager.send_message.return_value = mock_msg
    project_id = "22222222-2222-4222-8222-222222222222"
    session_uuid = "33333333-3333-4333-8333-333333333333"
    db = MagicMock()
    db.fetchone.return_value = {"id": session_uuid}
    registry: Any = create_communications_registry(mock_manager, db=db, workspace_root=tmp_path)
    inline_keyboard = [[{"text": "Ship", "value": "ship"}]]

    with patch(
        "gobby.utils.project_context.get_project_context",
        return_value={"id": project_id},
    ):
        result = await registry.get_tool("send_message")(
            channel="telegram",
            content="Ship?",
            session_id="#14069",
            inline_keyboard=inline_keyboard,
        )

    assert result["success"] is True
    sent = mock_manager.send_message.await_args.kwargs
    assert sent["session_id"] == session_uuid
    metadata = sent["metadata"]
    assert metadata["callback_project_id"] == project_id
    assert metadata["inline_keyboard"] == inline_keyboard


@pytest.mark.asyncio
async def test_send_message_resolves_hash_n_session_to_uuid(
    mock_manager: MagicMock,
    tmp_path: Path,
) -> None:
    """A #N session_id is stored as that project's session UUID."""
    project_id = "22222222-2222-4222-8222-222222222222"
    session_uuid = "11111111-1111-4111-8111-111111111111"
    db = MagicMock()
    db.fetchone.return_value = {"id": session_uuid}
    registry: Any = create_communications_registry(mock_manager, db=db, workspace_root=tmp_path)
    mock_manager.send_message.return_value = MagicMock(id="msg-123", status="sent", error=None)

    with patch(
        "gobby.utils.project_context.get_project_context",
        return_value={"id": project_id},
    ):
        result = await registry.get_tool("send_message")(
            channel="telegram",
            content="Ship?",
            session_id="#14069",
        )

    assert result["success"] is True
    assert mock_manager.send_message.await_args.kwargs["session_id"] == session_uuid
    db.fetchone.assert_called_once_with(
        "SELECT id FROM sessions WHERE project_id = %s AND seq_num = %s",
        (project_id, 14069),
    )


@pytest.mark.asyncio
async def test_send_message_refuses_unknown_hash_n_session(
    mock_manager: MagicMock,
    tmp_path: Path,
) -> None:
    """An unresolved #N fails at send time instead of storing the literal."""
    project_id = "22222222-2222-4222-8222-222222222222"
    db = MagicMock()
    db.fetchone.return_value = None
    registry: Any = create_communications_registry(mock_manager, db=db, workspace_root=tmp_path)
    mock_manager.send_message.return_value = MagicMock(id="msg-123", status="sent", error=None)

    with patch(
        "gobby.utils.project_context.get_project_context",
        return_value={"id": project_id},
    ):
        result = await registry.get_tool("send_message")(
            channel="telegram",
            content="Ship?",
            session_id="#14069",
        )

    assert result["success"] is False
    assert "14069" in result["error"]
    mock_manager.send_message.assert_not_awaited()
