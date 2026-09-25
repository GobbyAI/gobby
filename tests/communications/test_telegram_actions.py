from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.communications.models import ChannelConfig, CommsMessage
from gobby.communications.telegram_actions import TelegramActionController
from gobby.sessions.mailbox import MailboxSendResult
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 7, 30, 20, 0, tzinfo=UTC)
SESSION_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "22222222-2222-4222-8222-222222222222"


def _channel() -> ChannelConfig:
    return ChannelConfig(
        id="33333333-3333-4333-8333-333333333333",
        channel_type="telegram",
        name="telegram-main",
        enabled=True,
        config_json={"allow_from": ["42"]},
        created_at=NOW,
        updated_at=NOW,
    )


def _message(
    *,
    content: str,
    content_type: str = "text",
    metadata: dict[str, object] | None = None,
    session_id: str = SESSION_ID,
) -> CommsMessage:
    return CommsMessage(
        id=f"message-{content_type}-{content}",
        channel_id=_channel().id,
        direction="inbound",
        content=content,
        content_type=content_type,
        platform_message_id=f"platform-{content_type}-{content}",
        session_id=session_id,
        identity_id="stored-identity",
        metadata_json={
            "chat_id": "chat-1",
            "conversation_type": "private",
            "external_user_id": "42",
            **(metadata or {}),
        },
        created_at=NOW,
    )


def _source_message() -> CommsMessage:
    return CommsMessage(
        id="source-message",
        channel_id=_channel().id,
        direction="outbound",
        content="Index docs - Paused",
        platform_message_id="900",
        session_id=SESSION_ID,
        metadata_json={
            "platform_destination": "chat-1",
        },
        created_at=NOW,
    )


def _controller() -> tuple[TelegramActionController, MagicMock, MagicMock, MagicMock]:
    channel = _channel()
    manager = MagicMock()
    manager.get_channel_by_name.return_value = channel
    manager.admit_inbound_message = AsyncMock(return_value=True)
    manager.send_message = AsyncMock(return_value=[])
    manager.store.get_message_by_platform_id.return_value = _source_message()
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(
        id=SESSION_ID,
        status="paused",
        project_id=PROJECT_ID,
    )
    mailbox = MagicMock()
    mailbox.send = AsyncMock(return_value=MailboxSendResult(wake_results=[]))
    return (
        TelegramActionController(
            manager,
            session_manager,
            mailbox,
        ),
        manager,
        session_manager,
        mailbox,
    )


@pytest.mark.parametrize(
    "content_type,metadata",
    [
        ("text", {"reply_to_message_id": "ordinary-post"}),
        ("callback", {"callback_value": "approved", "callback_session_id": SESSION_ID}),
        ("text", {}),
    ],
)
async def test_live_session_inbound_uses_mailbox_without_responder_event(
    content_type: str, metadata: dict[str, object]
) -> None:
    controller, manager, sessions, mailbox = _controller()
    sessions.get.return_value = SimpleNamespace(id=SESSION_ID, status="active", source="claude")
    manager.store.get_message_by_platform_id.return_value = (
        CommsMessage(
            id="ordinary-post",
            channel_id=_channel().id,
            direction="outbound",
            content="Original post",
            metadata_json={"platform_destination": "chat-1"},
            created_at=NOW,
        )
        if "reply_to_message_id" in metadata
        else None
    )
    message = _message(content="answer", content_type=content_type, metadata=metadata)

    assert await controller.handle(_channel().name, message) is True

    mailbox.send.assert_awaited_once()
    delivery = mailbox.send.await_args.kwargs
    assert delivery["target_id"] == SESSION_ID
    assert delivery["wake"] is True
    assert delivery["metadata"]["channel"] == _channel().name
    assert delivery["metadata"]["sender"] == "42"
    assert delivery["metadata"]["reply_to_message_id"] == metadata.get("reply_to_message_id")
    assert delivery["metadata"]["replied_to_post"] == (
        "Original post" if "reply_to_message_id" in metadata else None
    )
    assert delivery["metadata"]["callback_data"] == metadata.get("callback_value")
    manager.send_message.assert_not_awaited()


async def test_button_tap_addressed_by_hash_ref_reaches_the_live_session() -> None:
    """session_id #N is the ref Josh passes. The tap still reaches that live session."""
    controller, _manager, sessions, mailbox = _controller()
    live = SimpleNamespace(id=SESSION_ID, status="active", source="claude")
    sessions.resolve_session_reference.return_value = SESSION_ID

    def _get(session_id: str) -> SimpleNamespace | None:
        return live if session_id == SESSION_ID else None

    sessions.get.side_effect = _get
    message = _message(
        content="ship",
        content_type="callback",
        session_id="#14069",
        metadata={
            "callback_value": "ship",
            "callback_session_id": "#14069",
            "callback_project_id": PROJECT_ID,
        },
    )

    assert await controller.handle(_channel().name, message) is True

    sessions.resolve_session_reference.assert_called_once_with("#14069", PROJECT_ID)
    mailbox.send.assert_awaited_once()
    assert mailbox.send.await_args.kwargs["target_id"] == SESSION_ID
    assert mailbox.send.await_args.kwargs["content"] == "ship"


async def test_inbound_attachment_reaches_the_live_session_with_its_file() -> None:
    """A photo or document is stored on disk. The live session gets that path."""
    controller, manager, sessions, mailbox = _controller()
    sessions.get.return_value = SimpleNamespace(id=SESSION_ID, status="active", source="claude")
    manager.store.list_attachments.return_value = [
        SimpleNamespace(
            filename="notes.txt",
            content_type="text/plain",
            local_path="/files/notes.txt",
        )
    ]
    message = _message(
        content="see notes",
        content_type="attachment",
        metadata={"telegram_attachment": {"file_id": "file-1", "media_type": "document"}},
    )

    assert await controller.handle(_channel().name, message) is True

    manager.store.list_attachments.assert_called_once_with(message.id)
    delivery = mailbox.send.await_args.kwargs
    assert delivery["target_id"] == SESSION_ID
    assert delivery["content"] == "see notes"
    assert delivery["metadata"]["attachments"] == [
        {
            "filename": "notes.txt",
            "content_type": "text/plain",
            "local_path": "/files/notes.txt",
        }
    ]


async def test_comms_session_inbound_remains_for_responder() -> None:
    controller, _, sessions, mailbox = _controller()
    sessions.get.return_value = SimpleNamespace(id=SESSION_ID, status="active", source="comms")

    assert await controller.handle(_channel().name, _message(content="plain")) is False
    mailbox.send.assert_not_awaited()


async def test_mailbox_failure_cannot_start_responder_turn() -> None:
    controller, manager, sessions, mailbox = _controller()
    sessions.get.return_value = SimpleNamespace(id=SESSION_ID, status="active", source="codex")
    mailbox.send.side_effect = RuntimeError("mailbox unavailable")

    assert await controller.handle(_channel().name, _message(content="reply")) is True
    manager.send_message.assert_awaited_once()
    assert "send your message again" in manager.send_message.await_args.args[1]


async def test_agent_command_lists_live_agents_and_marks_current_target() -> None:
    controller, manager, sessions, _ = _controller()
    other_id = "44444444-4444-4444-8444-444444444444"
    sessions.list.return_value = [
        SimpleNamespace(id=SESSION_ID, status="active", title="Assistant", source="claude"),
        SimpleNamespace(id=other_id, status="paused", title="Lane Developer", source="codex"),
    ]
    manager.attached_session.return_value = SESSION_ID

    consumed = await controller.handle(_channel().name, _message(content="/agent"))

    assert consumed is True
    menu = manager.send_message.await_args
    keyboard = menu.kwargs["metadata"]["inline_keyboard"]
    assert [button["text"] for row in keyboard for button in row] == [
        "✓ Assistant",
        "Lane Developer",
    ]
    assert all("#" not in button["text"] for row in keyboard for button in row)
    assert menu.kwargs["metadata"]["callback_action"] == "agent_target"
    assert menu.kwargs["session_id"] is None


async def test_agent_menu_fits_ten_agents_without_next_button() -> None:
    controller, manager, sessions, _ = _controller()
    agents = [
        SimpleNamespace(
            id=f"00000000-0000-4000-8000-{index:012d}",
            status="active",
            title=f"Agent {index}",
            source="codex",
        )
        for index in range(10)
    ]
    sessions.list.return_value = agents
    manager.attached_session.return_value = agents[0].id

    await controller.handle(_channel().name, _message(content="/agent list"))

    keyboard = manager.send_message.await_args.kwargs["metadata"]["inline_keyboard"]
    assert len(keyboard) == 5
    assert all(len(row) == 2 for row in keyboard)
    assert keyboard[0][0]["text"] == "✓ Agent 0"
    assert all(button["text"] not in {"Next", "Previous"} for row in keyboard for button in row)


async def test_agent_menu_shortens_and_disambiguates_duplicate_titles() -> None:
    controller, manager, sessions, _ = _controller()
    title = "A very long Telegram agent title " * 4
    sessions.list.return_value = [
        SimpleNamespace(id=SESSION_ID, seq_num=12, status="active", title=title, source="codex"),
        SimpleNamespace(
            id="44444444-4444-4444-8444-444444444444",
            seq_num=13,
            status="active",
            title=title,
            source="codex",
        ),
    ]

    await controller.handle(_channel().name, _message(content="/agent"))

    keyboard = manager.send_message.await_args.kwargs["metadata"]["inline_keyboard"]
    labels = [button["text"] for row in keyboard for button in row]
    assert labels[0] != labels[1]
    assert labels[0].endswith("#12")
    assert labels[1].endswith("#13")
    assert all(len(label) <= 48 for label in labels)


async def test_agent_command_lists_a_real_clear_successor(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = SessionManager(temp_db)
    predecessor = sessions.register(
        external_id="telegram-clear-predecessor",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
        title="Assistant",
    )
    successor = sessions.register(
        external_id="telegram-clear-successor",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
        title="Lane 4",
        parent_session_id=predecessor.id,
        agent_depth=0,
    )
    controller, manager, _, _ = _controller()
    controller._session_manager = sessions
    manager.attached_session.return_value = successor.id

    await controller.handle(_channel().name, _message(content="/agent"))

    labels = [
        button["text"]
        for row in manager.send_message.await_args.kwargs["metadata"]["inline_keyboard"]
        for button in row
    ]
    assert "✓ Lane 4" in labels


async def test_agent_command_is_published_in_telegram_menu() -> None:
    from gobby.communications.commands import telegram_bot_commands

    commands = {item["command"] for item in telegram_bot_commands()}
    assert "agent" in commands
    assert "subscriptions" not in commands


async def test_agent_command_reports_when_no_agents_are_running() -> None:
    controller, manager, sessions, _ = _controller()
    sessions.list.return_value = []
    manager.attached_session.return_value = None

    await controller.handle(_channel().name, _message(content="/agent"))

    assert manager.send_message.await_args.args[1] == "No agents are running."
    assert manager.send_message.await_args.kwargs["metadata"]["inline_keyboard"] == []


async def test_agent_command_rejects_wildcard_only_sender_allowlist() -> None:
    controller, manager, _, _ = _controller()
    channel = _channel()
    channel.config_json["allow_from"] = ["*"]
    manager.get_channel_by_name.return_value = channel

    await controller.handle(channel.name, _message(content="/agent"))

    assert "authorized private chat" in manager.send_message.await_args.args[1]
    assert "inline_keyboard" not in manager.send_message.await_args.kwargs["metadata"]
    assert manager.send_message.await_args.kwargs["session_id"] is None


async def test_agent_button_switches_the_chat_target() -> None:
    controller, manager, sessions, _ = _controller()
    manager.edit_message = AsyncMock()
    target_id = "44444444-4444-4444-8444-444444444444"
    sessions.get.side_effect = lambda session_id: SimpleNamespace(
        id=session_id, status="active", source="codex", title="Lane Developer"
    )
    sessions.list.return_value = [
        SimpleNamespace(id=target_id, status="active", source="codex", title="Lane Developer")
    ]
    manager.attached_session.return_value = target_id
    source = _source_message()
    source.metadata_json.update(
        {"callback_action": "agent_target", "agent_channel_id": _channel().id}
    )
    manager.store.get_message_by_platform_id.return_value = source
    callback = _message(
        content="select",
        content_type="callback",
        metadata={
            "callback_action": "agent_target",
            "callback_source_message_id": "900",
            "callback_value": json.dumps(
                {"op": "set", "channel_id": _channel().id, "session_id": target_id}
            ),
        },
    )

    consumed = await controller.handle(_channel().name, callback)

    assert consumed is True
    manager.switch_conversation.assert_called_once_with(_channel().name, "dm:chat-1", target_id)
    manager.send_message.assert_awaited_once_with(
        _channel().name,
        "Active agent: Lane Developer",
        session_id=None,
        metadata={"platform_destination": "chat-1"},
    )
    manager.edit_message.assert_awaited_once_with(
        _channel().name,
        "900",
        "Active agent: Lane Developer\nChoose an agent:",
        "chat-1",
        inline_keyboard=[
            [
                {
                    "text": "✓ Lane Developer",
                    "value": json.dumps(
                        {
                            "op": "set",
                            "channel_id": _channel().id,
                            "session_id": target_id,
                            "page": 0,
                        }
                    ),
                }
            ]
        ],
    )


async def test_agent_switch_sends_fresh_menu_when_edit_fails() -> None:
    controller, manager, sessions, _ = _controller()
    manager.edit_message = AsyncMock(side_effect=RuntimeError("edit failed"))
    target_id = "44444444-4444-4444-8444-444444444444"
    target = SimpleNamespace(id=target_id, status="active", source="codex", title="Lane Developer")
    sessions.get.return_value = target
    sessions.list.return_value = [target]
    manager.attached_session.return_value = target_id
    source = _source_message()
    source.metadata_json.update(
        {"callback_action": "agent_target", "agent_channel_id": _channel().id}
    )
    manager.store.get_message_by_platform_id.return_value = source
    callback = _message(
        content="select",
        content_type="callback",
        metadata={
            "callback_action": "agent_target",
            "callback_source_message_id": "900",
            "callback_value": json.dumps(
                {"op": "set", "channel_id": _channel().id, "session_id": target_id}
            ),
        },
    )

    await controller.handle(_channel().name, callback)

    assert manager.send_message.await_count == 2
    assert "inline_keyboard" in manager.send_message.await_args_list[0].kwargs["metadata"]
    assert "Active agent: Lane Developer" in manager.send_message.await_args_list[1].args[1]
    assert "new menu" in manager.send_message.await_args_list[1].args[1]


async def test_agent_button_rejects_wildcard_only_sender_allowlist() -> None:
    controller, manager, _, _ = _controller()
    channel = _channel()
    channel.config_json["allow_from"] = ["*"]
    manager.get_channel_by_name.return_value = channel
    callback = _message(
        content="select",
        content_type="callback",
        metadata={"callback_action": "agent_target"},
    )

    assert await controller.handle(channel.name, callback)

    manager.switch_conversation.assert_not_called()
    assert "authorized private chat" in manager.send_message.await_args.args[1]
    assert manager.send_message.await_args.kwargs["session_id"] is None
