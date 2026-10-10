"""Rebind private Telegram targets when their session ends."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from gobby.communications.agent_labels import agent_label
from gobby.sessions.clear_continuation import CLEAR_ATTEMPT_VARIABLE
from gobby.storage.sessions import LIVE_SESSION_STATUSES
from gobby.storage.sessions._contested_expiry import read_session_variables

if TYPE_CHECKING:
    from gobby.communications.manager import CommunicationsManager
    from gobby.sessions.status_events import SessionStatusTransition
    from gobby.storage.session_models import Session

logger = logging.getLogger(__name__)


def _is_assistant(session: Session) -> bool:
    if (
        session.status not in LIVE_SESSION_STATUSES
        or session.source in {"comms", "web-chat", "web_chat", "system"}
        or session.agent_depth != 0
        or session.agent_run_id is not None
    ):
        return False
    label = agent_label(session).casefold()
    return label == "assistant" or label.startswith(
        ("assistant ", "assistant:", "assistant -", "assistant —")
    )


def _switch_if_current(
    manager: CommunicationsManager,
    channel_name: str,
    channel_id: str,
    conversation_id: str,
    ended_session_id: str,
    replacement_id: str,
) -> bool:
    with manager._attachment_lock:
        if manager._conversation_attachments.get((channel_id, conversation_id)) != ended_session_id:
            return False
        if manager.attached_session(channel_id, conversation_id) == ended_session_id:
            return False
        manager.switch_conversation(channel_name, conversation_id, replacement_id)
        return True


def _clear_if_ended(
    manager: CommunicationsManager,
    channel_name: str,
    channel_id: str,
    conversation_id: str,
    ended_session_id: str,
) -> bool:
    with manager._attachment_lock:
        if manager.attached_session(channel_id, conversation_id) == ended_session_id:
            return False
        return manager.clear_conversation_target(channel_name, conversation_id, ended_session_id)


async def fallback_telegram_targets(
    manager: CommunicationsManager, transition: SessionStatusTransition
) -> None:
    """Keep status notices flowing even if Telegram target recovery fails."""
    try:
        await _fallback_telegram_targets(manager, transition)
    except Exception:
        logger.exception("Failed to reconcile Telegram targets for %s", transition.session_id)


async def _fallback_telegram_targets(
    manager: CommunicationsManager, transition: SessionStatusTransition
) -> None:
    session = await asyncio.to_thread(manager._session_store.get, transition.session_id)
    if session is not None and session.status in LIVE_SESSION_STATUSES:
        # A queued expiry notification can arrive after the provider resumed.
        return
    resumable = (
        session is not None
        and session.status == "expired"
        and session.session_type == "terminal"
        and session.agent_run_id is None
    )
    successor: Session | None = None
    if resumable:
        variables = await asyncio.to_thread(
            read_session_variables,
            manager.store.db,
            transition.session_id,
            keys=(CLEAR_ATTEMPT_VARIABLE,),
        )
        # Marker-bearing clears retain the existing Assistant-pool fallback.
        resumable = not isinstance((variables or {}).get(CLEAR_ATTEMPT_VARIABLE), dict)
    if resumable and session is not None:
        children = await asyncio.to_thread(
            manager._session_store.find_children, transition.session_id
        )
        # Native /clear may link a successor without staging a handoff marker.
        # Same-depth terminal lineage distinguishes it from spawned children.
        clear_successors = [
            child
            for child in children
            if child.session_type == "terminal"
            and child.agent_depth == session.agent_depth
            and child.agent_run_id is None
            and child.source == session.source
            and child.machine_id == session.machine_id
            and child.project_id == session.project_id
        ]
        if clear_successors:
            resumable = False
            successor = next(
                (
                    child
                    for child in reversed(clear_successors)
                    if child.status in LIVE_SESSION_STATUSES
                ),
                None,
            )
    with manager._attachment_lock:
        attached = []
        for key, holder in list(manager._conversation_attachments.items()):
            if holder != transition.session_id:
                continue
            if resumable and key in manager._managed_telegram_targets:
                # Operator-owned CLIs can resume. Keep the durable selection while
                # attached_session excludes expired holders from live delivery.
                continue
            if key[1].startswith("dm:") or key in manager._managed_telegram_targets:
                attached.append(key)
            else:
                manager._conversation_attachments.pop(key, None)
    if not attached:
        return

    sessions = await asyncio.to_thread(
        manager._session_store.list,
        statuses=list(LIVE_SESSION_STATUSES),
        limit=1000,
    )
    assistants = sorted(
        (session for session in sessions if _is_assistant(session)),
        key=lambda session: session.project_id != transition.project_id,
    )
    targets = ([successor] if successor is not None else []) + assistants
    for channel_id, conversation_id in attached:
        try:
            channel = await asyncio.to_thread(manager.get_channel, channel_id)
            if channel is None or channel.channel_type != "telegram":
                continue
            rebound = False
            for assistant in targets:
                try:
                    switched = await asyncio.to_thread(
                        _switch_if_current,
                        manager,
                        channel.name,
                        channel.id,
                        conversation_id,
                        transition.session_id,
                        assistant.id,
                    )
                except ValueError:
                    continue
                rebound = True
                if not switched:
                    logger.debug("Telegram target changed before fallback for %s", conversation_id)
                break
            if rebound:
                continue

            cleared = await asyncio.to_thread(
                _clear_if_ended,
                manager,
                channel.name,
                channel.id,
                conversation_id,
                transition.session_id,
            )
            if not cleared:
                continue
            kind, _, address = conversation_id.partition(":")
            chat_id, _, thread_id = address.partition(":")
            if not chat_id:
                continue
            metadata = {"platform_destination": chat_id}
            if kind == "topic" and thread_id:
                metadata["thread_id"] = thread_id
            notice = (
                "No Assistant is available for this chat. Use /agent to choose another agent."
                if assistants
                else "No Assistant is running. Use /agent when one is available."
            )
            await manager.send_message(channel.name, notice, metadata=metadata)
        except Exception:
            logger.exception("Failed to rebind Telegram conversation %s", conversation_id)
