"""Keep the enrolled operator's Telegram DM on terminal seats across lifecycle changes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from gobby.communications.models import ChannelConfig, CommsMessage
from gobby.communications.telegram_access import allowed_senders, is_telegram_dm
from gobby.storage.sessions import LIVE_SESSION_STATUSES
from gobby.storage.sessions._contested_expiry import read_session_variables

if TYPE_CHECKING:
    from gobby.communications.manager import CommunicationsManager
    from gobby.storage.session_models import Session


def _operator_sender(channel: ChannelConfig) -> str | None:
    project = channel.config_json.get("operator_project_id")
    senders = allowed_senders(channel.config_json)
    if (
        channel.channel_type == "telegram"
        and isinstance(project, str)
        and project
        and len(senders) == 1
    ):
        return next(iter(senders))
    return None


def is_operator_dm(channel: ChannelConfig, message: CommsMessage) -> bool:
    """Recognize the enrolled operator before and after identity resolution."""
    if not is_telegram_dm(channel, message):
        return False
    sender = message.metadata_json.get("external_user_id", message.identity_id)
    return sender is not None and sender == _operator_sender(channel)


def is_terminal_session(session: Session) -> bool:
    """Provider names alone do not distinguish a CLI from a web-chat runtime."""
    return (
        session.source not in {"comms", "web-chat", "web_chat", "system"}
        and getattr(session, "session_type", "terminal") != "web_chat"
    )


def reconcile_operator_target(manager: CommunicationsManager, channel: ChannelConfig) -> str | None:
    """Retain an operator selection, otherwise attach the live canonical Assistant."""
    if _operator_sender(channel) is None:
        return None
    try:
        conversation = manager.resolve_operator_conversation(channel.name)["conversation_id"]
    except ValueError:
        return None
    if conversation is None:
        return None
    with manager._attachment_lock:
        holder_id = manager._conversation_attachments.get((channel.id, conversation))
        holder = manager._session_store.get(holder_id) if holder_id else None
        if holder and holder.status in LIVE_SESSION_STATUSES and is_terminal_session(holder):
            return holder.id
        project_id = channel.config_json.get("operator_project_id")
        if not isinstance(project_id, str) or not project_id:
            return None
        sessions = manager._session_store.list(
            project_id=project_id, statuses=list(LIVE_SESSION_STATUSES), limit=1000
        )
        # A provider may resume while the candidate query is in flight.
        holder = manager._session_store.get(holder_id) if holder_id else None
        if holder and holder.status in LIVE_SESSION_STATUSES and is_terminal_session(holder):
            return holder.id
        if holder_id:
            manager.clear_conversation_target(channel.name, conversation, holder_id)
        for session in sessions:
            if (
                not is_terminal_session(session)
                or session.agent_depth != 0
                or session.agent_run_id is not None
            ):
                continue
            variables = read_session_variables(manager.store.db, session.id, keys=("_agent_type",))
            if (variables or {}).get("_agent_type") != "assistant":
                continue
            try:
                manager.attach_conversation(channel.name, conversation, session.id)
            except ValueError:
                continue  # An Assistant already serving another conversation is unavailable.
            return session.id
    return None


async def drain_operator_messages(manager: CommunicationsManager, channel: ChannelConfig) -> None:
    """Retry durable operator input without ever invoking the chat responder."""
    if _operator_sender(channel) is None:
        return
    lock = manager._telegram_binding_locks.setdefault(channel.id, asyncio.Lock())
    async with lock:
        target = await asyncio.to_thread(reconcile_operator_target, manager, channel)
        pending = await asyncio.to_thread(manager.store.recover_operator_messages, channel.id)
        for message in pending:
            session_id = target
            if message.metadata_json.get("session_route") == "reply" and message.session_id:
                replied = await asyncio.to_thread(manager._session_store.get, message.session_id)
                if (
                    replied
                    and replied.status in LIVE_SESSION_STATUSES
                    and is_terminal_session(replied)
                ):
                    session_id = replied.id
            if session_id is None:
                continue
            message.session_id = session_id
            if message.metadata_json.get("session_route") != "reply":
                message.metadata_json["session_route"] = "attached"
            await manager.handle_session_action(channel.name, message)
            if message.metadata_json.get("operator_pending"):
                break  # Preserve input order when mailbox delivery fails.
