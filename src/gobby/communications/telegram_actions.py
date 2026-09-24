"""Telegram controls for actionable lifecycle messages and event subscriptions."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from gobby.communications.agent_labels import agent_label
from gobby.communications.models import ChannelConfig, CommsMessage, CommsRoutingRule
from gobby.communications.native_plan_actions import decode_native_plan_option
from gobby.communications.telegram_access import allowed_senders
from gobby.storage.session_resolution import is_session_uuid
from gobby.storage.sessions import LIVE_SESSION_STATUSES, system_session_id

if TYPE_CHECKING:
    from gobby.communications.manager import CommunicationsManager
    from gobby.communications.native_plan_actions import NativePlanActionService
    from gobby.sessions.mailbox import MailboxService
    from gobby.storage.sessions import SessionManager

_PAGE_SIZE = 6
_SESSION_ACTION = "session_action"
_SUBSCRIPTION_ACTION = "subscription_control"
_AGENT_TARGET_ACTION = "agent_target"

logger = logging.getLogger(__name__)


class TelegramActionController:
    """Consume Telegram session actions before generic responder fan-out."""

    def __init__(
        self,
        manager: CommunicationsManager,
        session_manager: SessionManager,
        mailbox: MailboxService,
        native_plan_actions: NativePlanActionService | None = None,
    ) -> None:
        self._manager = manager
        self._session_manager = session_manager
        self._mailbox = mailbox
        self._native_plan_actions = native_plan_actions

    async def handle(self, channel_name: str, message: CommsMessage) -> bool:
        """Handle one persisted Telegram action when it belongs to this controller."""
        channel = self._manager.get_channel_by_name(channel_name)
        if channel is None or channel.channel_type != "telegram":
            return False

        action = message.metadata_json.get("callback_action")
        if message.content_type == "callback" and action == _SESSION_ACTION:
            await self._consume_safely(
                channel,
                message,
                self._handle_callback_action(channel, message),
            )
            return True
        if message.content_type == "callback" and action == _SUBSCRIPTION_ACTION:
            await self._consume_safely(
                channel,
                message,
                self._handle_subscription_callback(channel, message),
            )
            return True
        if message.content_type == "callback" and action == _AGENT_TARGET_ACTION:
            await self._consume_safely(
                channel,
                message,
                self._handle_agent_target_callback(channel, message),
            )
            return True
        if _command_name(message.content) == "subscriptions":
            await self._consume_safely(
                channel,
                message,
                self._handle_subscriptions_command(channel, message),
            )
            return True
        if _command_name(message.content) == "agent":
            await self._consume_safely(channel, message, self._send_agent_menu(channel, message))
            return True

        reply_id = _string_value(message.metadata_json.get("reply_to_message_id"))
        source = await self._source_message(channel.name, reply_id) if reply_id else None
        if source is not None and source.metadata_json.get("lifecycle_actionable"):
            await self._consume_safely(
                channel,
                message,
                self._deliver_session_answer(
                    channel,
                    message,
                    source,
                    message.content,
                    "reply",
                ),
            )
            return True

        session_ref = message.session_id
        session_id = session_ref
        if session_ref and not is_session_uuid(session_ref):
            project_id = _string_value(message.metadata_json.get("callback_project_id"))
            try:
                session_id = await asyncio.to_thread(
                    self._session_manager.resolve_session_reference,
                    session_ref,
                    project_id,
                )
            except ValueError:
                logger.info(
                    "Telegram message %s did not resolve session reference %s",
                    message.id,
                    session_ref,
                )
                session_id = None
        session = (
            await asyncio.to_thread(self._session_manager.get, session_id) if session_id else None
        )
        if (
            session is not None
            and session.status in LIVE_SESSION_STATUSES
            and getattr(session, "source", "comms") not in {"comms", "web-chat", "web_chat"}
            and message.content_type != "reaction"
        ):
            try:
                safe_source = (
                    source if source is not None and _same_telegram_chat(source, message) else None
                )
                await self._deliver_inbound(channel, message, session.id, safe_source)
            except Exception:
                logger.exception(
                    "Failed to deliver Telegram message %s to %s", message.id, session.id
                )
                try:
                    await self._feedback(
                        channel, message, "Delivery failed. Please send your message again."
                    )
                except Exception:
                    logger.exception(
                        "Failed to report Telegram delivery failure for %s", message.id
                    )
            return True
        return False

    async def _deliver_inbound(
        self,
        channel: ChannelConfig,
        message: CommsMessage,
        session_id: str,
        source: CommsMessage | None,
    ) -> None:
        """Persist a Telegram message for its live CLI or agent recipient."""
        content = message.content or "[Telegram attachment]"
        metadata: dict[str, Any] = {
            "channel": channel.name,
            "sender": message.metadata_json.get("external_user_id"),
            "sender_username": message.metadata_json.get("external_username"),
            "communications_message_id": message.id,
            "telegram_chat_id": message.metadata_json.get("chat_id"),
            "telegram_platform_message_id": message.platform_message_id,
            "reply_to_message_id": message.metadata_json.get("reply_to_message_id"),
            "replied_to_post": source.content if source is not None else None,
            "callback_data": message.metadata_json.get("callback_value"),
        }
        if message.content_type == "attachment":
            stored = await asyncio.to_thread(self._manager.store.list_attachments, message.id)
            if isinstance(stored, list):
                references = [
                    {
                        "filename": attachment.filename,
                        "content_type": attachment.content_type,
                        "local_path": attachment.local_path,
                    }
                    for attachment in stored
                    if isinstance(attachment.local_path, str) and attachment.local_path
                ]
                if references:
                    metadata["attachments"] = references
        result = await self._mailbox.send(
            from_session_id=system_session_id(),
            target="session",
            target_id=session_id,
            content=content,
            wake=True,
            message_type="telegram_message",
            metadata=metadata,
            preserve_content=True,
        )
        if not result.success:
            raise RuntimeError(result.error or "Mailbox delivery was rejected")

    async def _consume_safely(
        self,
        channel: ChannelConfig,
        message: CommsMessage,
        action: Awaitable[None],
    ) -> None:
        try:
            await action
        except Exception:
            logger.exception("Failed to process Telegram action")
            try:
                await self._feedback(channel, message, "The action could not be delivered.")
            except Exception:
                logger.exception("Failed to send Telegram action error feedback")

    async def _handle_callback_action(
        self,
        channel: ChannelConfig,
        message: CommsMessage,
    ) -> None:
        source_id = _string_value(message.metadata_json.get("callback_source_message_id"))
        source = await self._source_message(channel.name, source_id)
        if source is None or not source.metadata_json.get("lifecycle_actionable"):
            await self._feedback(channel, message, "This session action is no longer available.")
            return
        callback_session_id = _string_value(message.metadata_json.get("callback_session_id"))
        source_session_id = _string_value(source.metadata_json.get("actionable_session_id"))
        if callback_session_id is None or callback_session_id != source_session_id:
            await self._feedback(channel, message, "This session action is invalid.")
            return
        answer = message.metadata_json.get("callback_value")
        if not isinstance(answer, str) or not answer.strip():
            await self._feedback(channel, message, "This session action is invalid.")
            return
        native_option = decode_native_plan_option(answer)
        if native_option is not None:
            await self._deliver_native_plan_option(
                channel,
                message,
                source,
                native_option,
            )
            return
        await self._deliver_session_answer(channel, message, source, answer, "button")

    async def _deliver_native_plan_option(
        self,
        channel: ChannelConfig,
        message: CommsMessage,
        source: CommsMessage,
        option: int,
    ) -> None:
        if not await self._admitted(channel, message):
            await self._feedback(channel, message, "This session action is not authorized.")
            return
        if not _same_telegram_chat(source, message):
            await self._feedback(channel, message, "This session action is invalid.")
            return

        target_id = _string_value(source.metadata_json.get("actionable_session_id"))
        session = (
            await asyncio.to_thread(self._session_manager.get, target_id)
            if target_id is not None
            else None
        )
        if session is None or session.status != "paused":
            await self._feedback(channel, message, "This session is no longer paused.")
            return
        source_project_id = _string_value(source.metadata_json.get("lifecycle_project_id"))
        if source_project_id is not None and source_project_id != session.project_id:
            await self._feedback(channel, message, "This session action is invalid.")
            return
        fingerprint = _string_value(source.metadata_json.get("native_plan_fingerprint"))
        if fingerprint is None or self._native_plan_actions is None:
            await self._feedback(channel, message, "This native plan action is unavailable.")
            return

        result = await self._native_plan_actions.dispatch(
            session.id,
            option=option,
            expected_fingerprint=fingerprint,
        )
        if result == "sent":
            await self._feedback(channel, message, "Sent.")
        elif result == "stale":
            await self._feedback(channel, message, "This plan prompt has changed.")
        elif result == "unavailable":
            await self._feedback(channel, message, "This native plan action is unavailable.")
        else:
            await self._feedback(channel, message, "The action could not be delivered.")

    async def _deliver_session_answer(
        self,
        channel: ChannelConfig,
        message: CommsMessage,
        source: CommsMessage,
        answer: str,
        action_kind: str,
    ) -> None:
        if not await self._admitted(channel, message):
            await self._feedback(channel, message, "This session action is not authorized.")
            return
        if not _same_telegram_chat(source, message):
            await self._feedback(channel, message, "This session action is invalid.")
            return

        target_id = _string_value(source.metadata_json.get("actionable_session_id"))
        session = (
            await asyncio.to_thread(self._session_manager.get, target_id)
            if target_id is not None
            else None
        )
        if session is None or session.status != "paused":
            await self._feedback(channel, message, "This session is no longer paused.")
            return
        source_project_id = _string_value(source.metadata_json.get("lifecycle_project_id"))
        if source_project_id is not None and source_project_id != session.project_id:
            await self._feedback(channel, message, "This session action is invalid.")
            return

        result = await self._mailbox.send(
            from_session_id=system_session_id(),
            target="session",
            target_id=session.id,
            content=answer,
            wake=True,
            message_type="telegram_session_action",
            metadata={
                "source": "telegram",
                "action_kind": action_kind,
                "communications_channel_id": channel.id,
                "communications_message_id": message.id,
                "telegram_chat_id": message.metadata_json.get("chat_id"),
                "telegram_platform_message_id": message.platform_message_id,
            },
            preserve_content=True,
        )
        delivered = any(item.get("delivered") is True for item in result.wake_results)
        await self._feedback(
            channel,
            message,
            "Sent." if delivered else "Queued for delivery.",
        )

    async def _handle_subscriptions_command(
        self,
        channel: ChannelConfig,
        message: CommsMessage,
    ) -> None:
        if not await self._subscription_authorized(channel, message):
            await self._feedback(
                channel,
                message,
                "Subscription controls require an authorized private chat.",
            )
            return
        await self._send_subscription_menu(channel, message, page=0)

    async def _send_agent_menu(
        self,
        channel: ChannelConfig,
        message: CommsMessage,
        *,
        page: int = 0,
    ) -> None:
        if not await self._subscription_authorized(channel, message):
            await self._feedback(
                channel, message, "Agent controls require an authorized private chat."
            )
            return
        conversation_id = _agent_conversation_key(message)
        if conversation_id is None:
            await self._feedback(channel, message, "This chat has no stable Telegram address.")
            return

        sessions = await asyncio.to_thread(
            self._session_manager.list,
            statuses=list(LIVE_SESSION_STATUSES),
            exclude_subagents=True,
            limit=1000,
        )
        agents = sorted(
            (
                session
                for session in sessions
                if session.status in LIVE_SESSION_STATUSES
                and session.source not in {"comms", "web-chat", "web_chat", "system"}
            ),
            key=lambda session: (agent_label(session).casefold(), session.id),
        )
        current = await asyncio.to_thread(
            self._manager.attached_session, channel.id, conversation_id
        )
        page_count = max(1, (len(agents) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        bounded_page = min(max(page, 0), page_count - 1)
        visible = agents[bounded_page * _PAGE_SIZE : (bounded_page + 1) * _PAGE_SIZE]
        keyboard = [
            [
                {
                    "text": f"{'✓ ' if agent.id == current else ''}{agent_label(agent)}",
                    "value": json.dumps(
                        {"op": "set", "channel_id": channel.id, "session_id": agent.id}
                    ),
                }
            ]
            for agent in visible
        ]
        if page_count > 1:
            navigation = []
            for label, next_page in (("Previous", bounded_page - 1), ("Next", bounded_page + 1)):
                if 0 <= next_page < page_count:
                    navigation.append(
                        {
                            "text": label,
                            "value": json.dumps(
                                {"op": "page", "channel_id": channel.id, "page": next_page}
                            ),
                        }
                    )
            keyboard.append(navigation)
        current_label = next(
            (agent_label(agent) for agent in agents if agent.id == current), "None"
        )
        menu_text = (
            f"Active agent: {current_label}\nChoose an agent:"
            if agents
            else "No agents are running."
        )
        await self._manager.send_message(
            channel.name,
            menu_text,
            session_id=message.session_id,
            metadata={
                **_reply_destination(message),
                "callback_action": _AGENT_TARGET_ACTION,
                "agent_channel_id": channel.id,
                "inline_keyboard": keyboard,
            },
        )

    async def _handle_agent_target_callback(
        self,
        channel: ChannelConfig,
        message: CommsMessage,
    ) -> None:
        if not await self._subscription_authorized(channel, message):
            await self._feedback(
                channel, message, "Agent controls require an authorized private chat."
            )
            return
        source_id = _string_value(message.metadata_json.get("callback_source_message_id"))
        source = await self._source_message(channel.name, source_id)
        if (
            source is None
            or source.metadata_json.get("callback_action") != _AGENT_TARGET_ACTION
            or source.metadata_json.get("agent_channel_id") != channel.id
            or not _same_telegram_chat(source, message)
        ):
            await self._feedback(channel, message, "This agent menu is invalid.")
            return
        raw_value = message.metadata_json.get("callback_value")
        try:
            payload = json.loads(raw_value) if isinstance(raw_value, str) else None
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict) or payload.get("channel_id") != channel.id:
            await self._feedback(channel, message, "This agent choice is invalid.")
            return
        if payload.get("op") == "page" and isinstance(payload.get("page"), int):
            await self._send_agent_menu(channel, message, page=payload["page"])
            return
        target_id = payload.get("session_id")
        if (
            payload.get("op") != "set"
            or not isinstance(target_id, str)
            or not is_session_uuid(target_id)
        ):
            await self._feedback(channel, message, "This agent choice is invalid.")
            return
        target = await asyncio.to_thread(self._session_manager.get, target_id)
        if (
            target is None
            or target.status not in LIVE_SESSION_STATUSES
            or target.source in {"comms", "web-chat", "web_chat", "system"}
        ):
            await self._feedback(channel, message, "This agent is no longer running.")
            return
        conversation_id = _agent_conversation_key(message)
        if conversation_id is None:
            await self._feedback(channel, message, "This chat has no stable Telegram address.")
            return
        try:
            await asyncio.to_thread(
                self._manager.switch_conversation, channel.name, conversation_id, target_id
            )
        except ValueError:
            await self._feedback(channel, message, "This agent is attached to another chat.")
            return
        await self._send_agent_menu(channel, message)

    async def _handle_subscription_callback(
        self,
        channel: ChannelConfig,
        message: CommsMessage,
    ) -> None:
        if not await self._subscription_authorized(channel, message):
            await self._feedback(
                channel,
                message,
                "Subscription controls require an authorized private chat.",
            )
            return
        source_id = _string_value(message.metadata_json.get("callback_source_message_id"))
        source = await self._source_message(channel.name, source_id)
        if (
            source is None
            or source.metadata_json.get("callback_action") != _SUBSCRIPTION_ACTION
            or not _same_telegram_chat(source, message)
            or source.metadata_json.get("subscription_channel_id") != channel.id
        ):
            await self._feedback(channel, message, "This subscription menu is invalid.")
            return

        payload = _subscription_payload(message.metadata_json.get("callback_value"))
        if payload is None or payload.get("channel_id") != channel.id:
            await self._feedback(channel, message, "This subscription action is invalid.")
            return

        page = _page_value(payload.get("page"))
        operation = payload.get("op")
        try:
            if operation == "set":
                await self._set_subscription(channel, payload)
            elif operation == "all":
                await self._set_all_subscriptions(channel, payload)
            elif operation != "page":
                await self._feedback(
                    channel,
                    message,
                    "This subscription action is invalid.",
                )
                return
        except (LookupError, ValueError):
            await self._feedback(channel, message, "This subscription action is invalid.")
            return
        await self._send_subscription_menu(channel, message, page=page)

    async def _set_subscription(
        self,
        channel: ChannelConfig,
        payload: dict[str, Any],
    ) -> None:
        subscription_id = _string_value(payload.get("subscription_id"))
        enabled = payload.get("enabled")
        if subscription_id is None or not isinstance(enabled, bool):
            raise ValueError("Invalid subscription state")
        subscription = await asyncio.to_thread(
            self._manager.get_event_subscription,
            subscription_id,
        )
        if subscription.channel_id != channel.id:
            raise ValueError("Subscription belongs to another channel")
        await asyncio.to_thread(
            self._manager.update_event_subscription,
            subscription.id,
            enabled=enabled,
        )

    async def _set_all_subscriptions(
        self,
        channel: ChannelConfig,
        payload: dict[str, Any],
    ) -> None:
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("Invalid subscription state")
        subscriptions = await asyncio.to_thread(
            self._manager.list_event_subscriptions,
            channel=channel.name,
        )
        for subscription in subscriptions:
            await asyncio.to_thread(
                self._manager.update_event_subscription,
                subscription.id,
                enabled=enabled,
            )

    async def _send_subscription_menu(
        self,
        channel: ChannelConfig,
        message: CommsMessage,
        *,
        page: int,
    ) -> None:
        subscriptions = await asyncio.to_thread(
            self._manager.list_event_subscriptions,
            channel=channel.name,
        )
        subscriptions.sort(key=lambda item: (item.name.casefold(), item.event_pattern, item.id))
        page_count = max(1, (len(subscriptions) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        bounded_page = min(max(page, 0), page_count - 1)
        start = bounded_page * _PAGE_SIZE
        visible = subscriptions[start : start + _PAGE_SIZE]

        keyboard = [
            [
                {
                    "text": _subscription_button_label(subscription),
                    "value": _subscription_value(
                        "set",
                        channel.id,
                        bounded_page,
                        subscription_id=subscription.id,
                        enabled=not subscription.enabled,
                    ),
                }
            ]
            for subscription in visible
        ]
        keyboard.append(
            [
                {
                    "text": "Enable all",
                    "value": _subscription_value(
                        "all",
                        channel.id,
                        bounded_page,
                        enabled=True,
                    ),
                },
                {
                    "text": "Disable all",
                    "value": _subscription_value(
                        "all",
                        channel.id,
                        bounded_page,
                        enabled=False,
                    ),
                },
            ]
        )
        if page_count > 1:
            navigation: list[dict[str, str]] = []
            if bounded_page > 0:
                navigation.append(
                    {
                        "text": "Previous",
                        "value": _subscription_value(
                            "page",
                            channel.id,
                            bounded_page - 1,
                        ),
                    }
                )
            if bounded_page < page_count - 1:
                navigation.append(
                    {
                        "text": "Next",
                        "value": _subscription_value(
                            "page",
                            channel.id,
                            bounded_page + 1,
                        ),
                    }
                )
            keyboard.append(navigation)

        lines = [f"Subscriptions ({bounded_page + 1}/{page_count})"]
        if visible:
            lines.extend(
                f"{'Enabled' if item.enabled else 'Disabled'} — {item.name} ({item.event_pattern})"
                for item in visible
            )
        else:
            lines.append("No subscriptions are attached to this channel.")
        await self._manager.send_message(
            channel.name,
            "\n\n".join(lines),
            session_id=message.session_id,
            metadata={
                **_reply_destination(message),
                "callback_action": _SUBSCRIPTION_ACTION,
                "inline_keyboard": keyboard,
                "subscription_channel_id": channel.id,
            },
        )

    async def _source_message(
        self,
        channel_name: str,
        platform_message_id: str | None,
    ) -> CommsMessage | None:
        if platform_message_id is None:
            return None
        return await asyncio.to_thread(
            self._manager.store.get_message_by_platform_id,
            channel_name,
            platform_message_id,
        )

    async def _admitted(
        self,
        channel: ChannelConfig,
        message: CommsMessage,
    ) -> bool:
        external_sender = _string_value(message.metadata_json.get("external_user_id"))
        if external_sender is None:
            return False
        candidate = replace(message, identity_id=external_sender)
        return await self._manager.admit_inbound_message(channel, candidate)

    async def _subscription_authorized(
        self,
        channel: ChannelConfig,
        message: CommsMessage,
    ) -> bool:
        if message.metadata_json.get("conversation_type") != "private":
            return False
        if not await self._admitted(channel, message):
            return False
        sender = _string_value(message.metadata_json.get("external_user_id"))
        allow_from = allowed_senders(channel.config_json)
        return sender is not None and (sender in allow_from or "*" in allow_from)

    async def _feedback(
        self,
        channel: ChannelConfig,
        message: CommsMessage,
        content: str,
    ) -> None:
        await self._manager.send_message(
            channel.name,
            content,
            session_id=message.session_id,
            metadata=_reply_destination(message),
        )


def _reply_destination(message: CommsMessage) -> dict[str, Any]:
    metadata: dict[str, Any] = {"platform_destination": message.metadata_json.get("chat_id")}
    thread_id = _string_value(message.metadata_json.get("message_thread_id"))
    if thread_id is not None:
        metadata["thread_id"] = thread_id
    return metadata


def _agent_conversation_key(message: CommsMessage) -> str | None:
    chat_id = _string_value(message.metadata_json.get("chat_id"))
    if chat_id is None:
        return None
    thread_id = _string_value(message.metadata_json.get("message_thread_id"))
    return f"topic:{chat_id}:{thread_id}" if thread_id else f"dm:{chat_id}"


def _same_telegram_chat(source: CommsMessage, inbound: CommsMessage) -> bool:
    destination = _string_value(source.metadata_json.get("platform_destination"))
    chat_id = _string_value(inbound.metadata_json.get("chat_id"))
    return destination is not None and destination == chat_id


def _command_name(content: str) -> str | None:
    token = content.strip().split(maxsplit=1)[0] if content.strip() else ""
    if not token.startswith("/"):
        return None
    return token[1:].split("@", maxsplit=1)[0].casefold() or None


def _subscription_button_label(subscription: CommsRoutingRule) -> str:
    verb = "Disable" if subscription.enabled else "Enable"
    return f"{verb} {subscription.name}"[:64]


def _subscription_value(
    operation: str,
    channel_id: str,
    page: int,
    *,
    subscription_id: str | None = None,
    enabled: bool | None = None,
) -> str:
    payload: dict[str, Any] = {
        "op": operation,
        "channel_id": channel_id,
        "page": page,
    }
    if subscription_id is not None:
        payload["subscription_id"] = subscription_id
    if enabled is not None:
        payload["enabled"] = enabled
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _subscription_payload(value: object) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _page_value(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _string_value(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    normalized = str(value).strip()
    return normalized or None
