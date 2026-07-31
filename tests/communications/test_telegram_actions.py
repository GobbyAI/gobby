from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.communications.inbound import InboundCommunications
from gobby.communications.models import ChannelConfig, CommsMessage, CommsRoutingRule
from gobby.communications.telegram_actions import TelegramActionController
from gobby.sessions.mailbox import MailboxSendResult

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
) -> CommsMessage:
    return CommsMessage(
        id=f"message-{content_type}-{content}",
        channel_id=_channel().id,
        direction="inbound",
        content=content,
        content_type=content_type,
        platform_message_id=f"platform-{content_type}-{content}",
        session_id=SESSION_ID,
        identity_id="stored-identity",
        metadata_json={
            "chat_id": "chat-1",
            "conversation_type": "private",
            "external_user_id": "42",
            **(metadata or {}),
        },
        created_at=NOW,
    )


def _source_message(
    *,
    actionable: bool = True,
    chat_id: str = "chat-1",
    native_plan_fingerprint: str | None = None,
) -> CommsMessage:
    return CommsMessage(
        id="source-message",
        channel_id=_channel().id,
        direction="outbound",
        content="Index docs - Paused",
        platform_message_id="900",
        session_id=SESSION_ID,
        metadata_json={
            "platform_destination": chat_id,
            "lifecycle_actionable": actionable,
            "actionable_session_id": SESSION_ID,
            "lifecycle_project_id": PROJECT_ID,
            **(
                {"native_plan_fingerprint": native_plan_fingerprint}
                if native_plan_fingerprint is not None
                else {}
            ),
        },
        created_at=NOW,
    )


def _controller(
    *,
    wake_results: list[dict[str, object]] | None = None,
    native_plan_actions: MagicMock | None = None,
) -> tuple[TelegramActionController, MagicMock, MagicMock, MagicMock]:
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
    mailbox.send = AsyncMock(return_value=MailboxSendResult(wake_results=wake_results or []))
    return (
        TelegramActionController(
            manager,
            session_manager,
            mailbox,
            native_plan_actions,
        ),
        manager,
        session_manager,
        mailbox,
    )


async def test_continue_callback_delivers_exact_answer_and_reports_live_wake() -> None:
    controller, manager, _, mailbox = _controller(
        wake_results=[{"session_id": SESSION_ID, "delivered": True}]
    )
    callback = _message(
        content="Continue",
        content_type="callback",
        metadata={
            "callback_action": "session_action",
            "callback_source_message_id": "900",
            "callback_session_id": SESSION_ID,
            "callback_value": "Continue",
        },
    )

    consumed = await controller.handle(_channel().name, callback)

    assert consumed is True
    mailbox.send.assert_awaited_once()
    send = mailbox.send.await_args.kwargs
    assert send["target_id"] == SESSION_ID
    assert send["content"] == "Continue"
    assert send["include_wakeup"] is True
    assert send["preserve_content"] is True
    assert send["metadata"]["action_kind"] == "button"
    manager.send_message.assert_awaited_once()
    assert manager.send_message.await_args.args[1] == "Sent."


async def test_native_plan_callback_dispatches_exact_option_without_mailbox() -> None:
    native_plan_actions = MagicMock()
    native_plan_actions.dispatch = AsyncMock(return_value="sent")
    controller, manager, _, mailbox = _controller(native_plan_actions=native_plan_actions)
    manager.store.get_message_by_platform_id.return_value = _source_message(
        native_plan_fingerprint="pane-fingerprint"
    )
    callback = _message(
        content="Yes, clear context and implement",
        content_type="callback",
        metadata={
            "callback_action": "session_action",
            "callback_source_message_id": "900",
            "callback_session_id": SESSION_ID,
            "callback_value": "native-plan:2",
        },
    )

    consumed = await controller.handle(_channel().name, callback)

    assert consumed is True
    native_plan_actions.dispatch.assert_awaited_once_with(
        SESSION_ID,
        option=2,
        expected_fingerprint="pane-fingerprint",
    )
    mailbox.send.assert_not_awaited()
    assert manager.send_message.await_args.args[1] == "Sent."


async def test_native_plan_callback_rejects_changed_prompt() -> None:
    native_plan_actions = MagicMock()
    native_plan_actions.dispatch = AsyncMock(return_value="stale")
    controller, manager, _, mailbox = _controller(native_plan_actions=native_plan_actions)
    manager.store.get_message_by_platform_id.return_value = _source_message(
        native_plan_fingerprint="pane-fingerprint"
    )
    callback = _message(
        content="Yes, implement this plan",
        content_type="callback",
        metadata={
            "callback_action": "session_action",
            "callback_source_message_id": "900",
            "callback_session_id": SESSION_ID,
            "callback_value": "native-plan:1",
        },
    )

    await controller.handle(_channel().name, callback)

    mailbox.send.assert_not_awaited()
    assert manager.send_message.await_args.args[1] == "This plan prompt has changed."


async def test_native_reply_to_any_persisted_chunk_preserves_text_and_reports_queue() -> None:
    controller, manager, _, mailbox = _controller()
    manager.store.get_message_by_platform_id.return_value = _source_message()
    reply = _message(
        content="  custom answer\n",
        metadata={"reply_to_message_id": "902"},
    )

    consumed = await controller.handle(_channel().name, reply)

    assert consumed is True
    assert mailbox.send.await_args.kwargs["content"] == "  custom answer\n"
    assert mailbox.send.await_args.kwargs["metadata"]["action_kind"] == "reply"
    assert manager.store.get_message_by_platform_id.call_args.args == (
        _channel().name,
        "902",
    )
    assert manager.send_message.await_args.args[1] == "Queued for delivery."


@pytest.mark.parametrize(
    ("status", "source_chat"),
    [
        ("active", "chat-1"),
        ("paused", "unrelated-chat"),
    ],
)
async def test_session_action_rejects_stale_or_unrelated_target(
    status: str,
    source_chat: str,
) -> None:
    controller, manager, session_manager, mailbox = _controller()
    session_manager.get.return_value.status = status
    manager.store.get_message_by_platform_id.return_value = _source_message(chat_id=source_chat)
    callback = _message(
        content="Fast",
        content_type="callback",
        metadata={
            "callback_action": "session_action",
            "callback_source_message_id": "900",
            "callback_session_id": SESSION_ID,
            "callback_value": "Fast",
        },
    )

    consumed = await controller.handle(_channel().name, callback)

    assert consumed is True
    mailbox.send.assert_not_awaited()
    assert "invalid" in manager.send_message.await_args.args[1].lower() or (
        "no longer paused" in manager.send_message.await_args.args[1].lower()
    )


async def test_consumed_action_is_suppressed_before_generic_responder_callback() -> None:
    channel = _channel()
    manager = MagicMock()
    manager._channel_by_name = {channel.name: channel}
    manager._adapters = {}
    manager.admit_inbound_message = AsyncMock(return_value=True)
    manager._store.get_message_by_platform_id.return_value = None
    manager._store.create_message.side_effect = lambda message: message
    manager.handle_session_action = AsyncMock(return_value=True)
    manager.event_callback = AsyncMock()
    manager.get_voice_transcriber.return_value = None
    manager.get_vision_extract_service.return_value = None
    inbound = InboundCommunications(manager)
    message = _message(
        content="Continue",
        content_type="callback",
        metadata={"callback_status": "ok"},
    )
    message.platform_message_id = None
    message.identity_id = None

    handled = await inbound.handle_messages(channel.name, [message])

    assert handled == [message]
    assert message.channel_id == channel.id
    assert message.metadata_json["platform_channel_id"] == channel.id
    assert message.content == "Continue"
    manager.handle_session_action.assert_awaited_once_with(channel.name, message)
    manager.event_callback.assert_not_awaited()


async def test_action_remains_consumed_when_error_feedback_delivery_fails() -> None:
    controller, manager, _, _ = _controller()
    manager.store.get_message_by_platform_id.return_value = None
    manager.send_message.side_effect = RuntimeError("Telegram unavailable")
    callback = _message(
        content="Continue",
        content_type="callback",
        metadata={
            "callback_action": "session_action",
            "callback_source_message_id": "missing",
            "callback_session_id": SESSION_ID,
            "callback_value": "Continue",
        },
    )

    consumed = await controller.handle(_channel().name, callback)

    assert consumed is True
    assert manager.send_message.await_count == 2


async def test_subscriptions_command_requires_allowlisted_private_chat() -> None:
    controller, manager, _, _ = _controller()
    command = _message(
        content="/subscriptions",
        metadata={"conversation_type": "group"},
    )

    consumed = await controller.handle(_channel().name, command)

    assert consumed is True
    assert "authorized private chat" in manager.send_message.await_args.args[1]
    manager.list_event_subscriptions.assert_not_called()


async def test_subscriptions_menu_paginates_six_rules_with_eight_rows_maximum() -> None:
    controller, manager, _, _ = _controller()
    manager.list_event_subscriptions.return_value = [
        CommsRoutingRule(
            id=f"rule-{index}",
            name=f"Rule {index}",
            channel_id=_channel().id,
            event_pattern=f"session.event.{index}",
            enabled=index % 2 == 0,
        )
        for index in range(7)
    ]

    consumed = await controller.handle(
        _channel().name,
        _message(content="/subscriptions"),
    )

    assert consumed is True
    call = manager.send_message.await_args
    assert call.args[1].startswith("Subscriptions (1/2)")
    keyboard = call.kwargs["metadata"]["inline_keyboard"]
    assert len(keyboard) == 8
    assert [button["text"] for button in keyboard[-1]] == ["Next"]
    assert call.kwargs["metadata"]["callback_action"] == "subscription_control"


async def test_subscription_callback_sets_explicit_state_and_sends_fresh_snapshot() -> None:
    controller, manager, _, _ = _controller()
    channel = _channel()
    subscription = CommsRoutingRule(
        id="rule-1",
        name="Paused sessions",
        channel_id=channel.id,
        event_pattern="session.agent.paused",
        enabled=False,
    )
    manager.get_event_subscription.return_value = subscription
    manager.list_event_subscriptions.return_value = [subscription]
    menu_source = _source_message()
    menu_source.metadata_json.update(
        {
            "callback_action": "subscription_control",
            "subscription_channel_id": channel.id,
        }
    )
    manager.store.get_message_by_platform_id.return_value = menu_source
    payload = json.dumps(
        {
            "op": "set",
            "channel_id": channel.id,
            "page": 0,
            "subscription_id": subscription.id,
            "enabled": True,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    callback = _message(
        content=payload,
        content_type="callback",
        metadata={
            "callback_action": "subscription_control",
            "callback_source_message_id": "900",
            "callback_value": payload,
        },
    )

    consumed = await controller.handle(channel.name, callback)

    assert consumed is True
    manager.update_event_subscription.assert_called_once_with(
        subscription.id,
        enabled=True,
    )
    assert manager.send_message.await_args.args[1].startswith("Subscriptions (1/1)")


async def test_subscription_callback_rejects_menu_from_another_channel() -> None:
    controller, manager, _, _ = _controller()
    menu_source = _source_message()
    menu_source.metadata_json.update(
        {
            "callback_action": "subscription_control",
            "subscription_channel_id": "other-channel",
        }
    )
    manager.store.get_message_by_platform_id.return_value = menu_source
    payload = json.dumps(
        {
            "op": "all",
            "channel_id": _channel().id,
            "page": 0,
            "enabled": True,
        }
    )
    callback = _message(
        content=payload,
        content_type="callback",
        metadata={
            "callback_action": "subscription_control",
            "callback_source_message_id": "900",
            "callback_value": payload,
        },
    )

    consumed = await controller.handle(_channel().name, callback)

    assert consumed is True
    manager.update_event_subscription.assert_not_called()
    assert manager.send_message.await_args.args[1] == "This subscription menu is invalid."
