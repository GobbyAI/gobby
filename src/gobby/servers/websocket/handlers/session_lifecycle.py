"""Session lifecycle handlers for WebSocket session control.

Handles stop_chat, clear_chat, delete_chat, and idle session cleanup.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.hooks.hook_types import SessionEndReason
from gobby.servers.chat_attachment_cleanup import cleanup_conversation_attachments
from gobby.servers.websocket.db import run_db
from gobby.servers.websocket.models import (
    CLEANUP_INTERVAL_SECONDS,
    IDLE_TIMEOUT_SECONDS,
)
from gobby.utils.json_helpers import json_dumps

if TYPE_CHECKING:
    from gobby.servers.websocket.session_control import SessionControlMixin

logger = logging.getLogger(__name__)


async def _delete_chat_attachments(
    mixin: SessionControlMixin,
    db: Any,
    conversation_id: str,
) -> None:
    """Delete attachment metadata and files for a cleared chat conversation."""

    await cleanup_conversation_attachments(
        db,
        conversation_id,
        run_db=lambda *args, **kwargs: run_db(mixin, *args, **kwargs),
        terminal=False,
    )


async def handle_stop_chat(
    mixin: SessionControlMixin, websocket: Any, data: dict[str, Any] | None = None
) -> None:
    """Handle stop_chat message to cancel the active chat stream.

    Message format:
    {
        "type": "stop_chat",
        "conversation_id": "optional-id"
    }
    """
    conversation_id = (data or {}).get("conversation_id")

    if conversation_id:
        await mixin._cancel_active_chat(conversation_id)
    else:
        # Legacy: stop all active chats (backwards compatibility)
        for conv_id in list(mixin._active_chat_tasks.keys()):
            await mixin._cancel_active_chat(conv_id)


async def handle_clear_chat(
    mixin: SessionControlMixin, websocket: Any, data: dict[str, Any]
) -> None:
    """Handle clear_chat message: stop session, mark completed, notify frontend.

    Message format:
    {
        "type": "clear_chat",
        "conversation_id": "stable-id"
    }
    """
    conversation_id = data.get("conversation_id")
    if not conversation_id:
        return

    session = mixin._chat_sessions.get(conversation_id)
    if not session:
        # No active session — just acknowledge
        await websocket.send(
            json_dumps({"type": "chat_cleared", "conversation_id": conversation_id})
        )
        return

    # Mark session as completed in database and clear pending plan
    if session.db_session_id:
        session_manager = getattr(mixin, "session_manager", None)
        if session_manager:
            try:
                await run_db(
                    mixin, session_manager.update, session.db_session_id, status="completed"
                )
            except Exception as e:
                logger.warning("Failed to update session status on clear: %s", e, exc_info=True)

    # Delete persisted chat messages
    session_manager = getattr(mixin, "session_manager", None)
    if session_manager and session_manager.db:
        try:
            from gobby.storage import chat_messages

            await _delete_chat_attachments(mixin, session_manager.db, conversation_id)
            await run_db(mixin, chat_messages.delete_messages, session_manager.db, conversation_id)
        except Exception as e:
            logger.warning("Failed to delete chat messages on clear: %s", e)

    # Fire SESSION_END before teardown
    await mixin._fire_session_end(conversation_id)

    # Stop the old ChatSession
    await mixin._cancel_active_chat(conversation_id)
    await session.stop()
    registry = getattr(mixin, "web_chat_session_registry", None)
    if registry is not None:
        registry.unregister(conversation_id)
    else:
        mixin._chat_sessions.pop(conversation_id, None)
    if hasattr(mixin, "_session_create_locks"):
        mixin._session_create_locks.pop(conversation_id, None)

    # Unload voice models if no sessions remain
    if hasattr(mixin, "_check_voice_idle"):
        await mixin._check_voice_idle()

    # Notify frontend
    await websocket.send(json_dumps({"type": "chat_cleared", "conversation_id": conversation_id}))
    logger.info("Chat cleared for conversation %s", conversation_id[:8])


async def handle_delete_chat(
    mixin: SessionControlMixin, websocket: Any, data: dict[str, Any]
) -> None:
    """Handle delete_chat message: stop session, delete from DB, notify frontend.

    Message format:
    {
        "type": "delete_chat",
        "conversation_id": "stable-id"
    }
    """
    conversation_id = data.get("conversation_id")
    if not conversation_id:
        return

    session = mixin._chat_sessions.get(conversation_id)
    db_session_id = getattr(session, "db_session_id", None) if session else None

    # Fall back to session_id from the message (for historical sessions not in memory)
    if not db_session_id:
        db_session_id = data.get("session_id")

    # Stop the ChatSession if active
    if session:
        await mixin._fire_session_end(conversation_id)
        await mixin._cancel_active_chat(conversation_id)
        await session.stop()
        registry = getattr(mixin, "web_chat_session_registry", None)
        if registry is not None:
            registry.unregister(conversation_id)
        else:
            mixin._chat_sessions.pop(conversation_id, None)
        if hasattr(mixin, "_session_create_locks"):
            mixin._session_create_locks.pop(conversation_id, None)

    # Unload voice models if no sessions remain
    if hasattr(mixin, "_check_voice_idle"):
        await mixin._check_voice_idle()

    # Soft-delete: mark as expired (preserves messages;
    # hard delete fails due to FK constraints from agent_runs, tasks, etc.)
    # Use 'expired' not 'handoff_ready' — no child session will pick these up.
    if db_session_id:
        session_manager = getattr(mixin, "session_manager", None)
        try:
            if session_manager:
                await run_db(mixin, session_manager.update, db_session_id, status="expired")
        except Exception as e:
            logger.warning("Failed to soft-delete session from DB: %s", e)

    # Notify frontend
    await websocket.send(json_dumps({"type": "chat_deleted", "conversation_id": conversation_id}))
    logger.info("Chat deleted for conversation %s", conversation_id[:8])


async def cleanup_idle_sessions(mixin: SessionControlMixin) -> None:
    """Periodically disconnect chat sessions that have been idle too long."""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            now = datetime.now(UTC)
            pending_config_updated_at = getattr(mixin, "_pending_config_updated_at", {})
            stale_pending_config = [
                conversation_id
                for conversation_id, updated_at in pending_config_updated_at.items()
                if (now - updated_at).total_seconds() > IDLE_TIMEOUT_SECONDS
            ]
            for conversation_id in stale_pending_config:
                for pending_name in (
                    "_pending_modes",
                    "_pending_projects",
                    "_pending_providers",
                    "_pending_agents",
                    "_pending_worktree_paths",
                ):
                    getattr(mixin, pending_name, {}).pop(conversation_id, None)
                pending_config_updated_at.pop(conversation_id, None)
            stale_sessions = [
                (conv_id, session)
                for conv_id, session in mixin._chat_sessions.items()
                if (now - session.last_activity).total_seconds() > IDLE_TIMEOUT_SECONDS
            ]
            cleaned_count = 0
            for conv_id, session in stale_sessions:
                # Fire SESSION_END before teardown (needs session in dict for lookup)
                await mixin._fire_session_end(conv_id, reason=SessionEndReason.IDLE)
                await mixin._cancel_active_chat(conv_id)
                # Awaited teardown may have allowed a replacement session to register.
                # Only remove the same stale session selected by this cleanup pass.
                if mixin._chat_sessions.get(conv_id) is not session:
                    continue
                registry = getattr(mixin, "web_chat_session_registry", None)
                if registry is not None:
                    registry.unregister(conv_id)
                else:
                    mixin._chat_sessions.pop(conv_id, None)
                if hasattr(mixin, "_session_create_locks"):
                    mixin._session_create_locks.pop(conv_id, None)
                await session.stop()
                cleaned_count += 1
                logger.debug("Cleaned up idle chat session %s", conv_id)
            if cleaned_count:
                logger.info("Cleaned up %s idle chat session(s)", cleaned_count)
                # Unload voice models if no sessions remain
                if hasattr(mixin, "_check_voice_idle"):
                    await mixin._check_voice_idle()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error in idle session cleanup")
