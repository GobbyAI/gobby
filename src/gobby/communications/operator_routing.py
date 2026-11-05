"""Keep the enrolled operator's Telegram DM on terminal seats across lifecycle changes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from gobby.communications.models import ChannelConfig, CommsMessage
from gobby.communications.telegram_access import allowed_senders, is_telegram_dm
from gobby.storage.sessions import LIVE_SESSION_STATUSES
from gobby.workflows.agent_resolver import session_has_capability

if TYPE_CHECKING:
    from gobby.communications.manager import CommunicationsManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.session_models import Session

OPERATOR_ATTACH_CAPABILITY = "auto_attach_operator_conversations"


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


def is_operator_seat(db: HubDatabase, session: Session) -> bool:
    """Live terminal the operator may select: unspawned, or granted by its definition."""
    if session.status not in LIVE_SESSION_STATUSES or not is_terminal_session(session):
        return False
    unspawned = (
        getattr(session, "agent_depth", 0) == 0 and getattr(session, "agent_run_id", None) is None
    )
    return unspawned or session_has_capability(db, session.id, OPERATOR_ATTACH_CAPABILITY)


def can_auto_attach(manager: CommunicationsManager, session: Session) -> bool:
    """Automatic attachment also needs the definition's grant, spawned or not."""
    db = manager.store.db
    return is_operator_seat(db, session) and session_has_capability(
        db, session.id, OPERATOR_ATTACH_CAPABILITY
    )


def reconcile_operator_target(manager: CommunicationsManager, channel: ChannelConfig) -> str | None:
    """Retain an operator selection, otherwise attach a live definition-authorized seat."""
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
            if not can_auto_attach(manager, session):
                continue
            try:
                manager.attach_conversation(channel.name, conversation, session.id)
            except ValueError:
                continue  # A seat already serving another conversation is unavailable.
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
