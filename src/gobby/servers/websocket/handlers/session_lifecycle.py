"""Session lifecycle handlers for WebSocket session control.

Handles stop_chat, clear_chat, delete_chat, and idle session cleanup.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.servers.websocket.models import (
    CLEANUP_INTERVAL_SECONDS,
    IDLE_TIMEOUT_SECONDS,
)

if TYPE_CHECKING:
    from gobby.servers.websocket.session_control import SessionControlMixin

logger = logging.getLogger(__name__)


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
            json.dumps({"type": "chat_cleared", "conversation_id": conversation_id})
        )
        return

    # Mark session as completed in database and clear pending plan
    if session.db_session_id:
        session_manager = getattr(mixin, "session_manager", None)
        if session_manager:
            try:
                await asyncio.to_thread(
                    session_manager.update, session.db_session_id, status="completed"
                )
            except Exception as e:
                logger.warning(f"Failed to update session status on clear: {e}", exc_info=True)

    # Delete persisted chat messages
    session_manager = getattr(mixin, "session_manager", None)
    if session_manager and session_manager.db:
        try:
            from gobby.storage import chat_messages

            await asyncio.to_thread(
                chat_messages.delete_messages, session_manager.db, conversation_id
            )
        except Exception as e:
            logger.warning(f"Failed to delete chat messages on clear: {e}")

    # Fire SESSION_END before teardown
    await mixin._fire_session_end(conversation_id)

    # Stop the old ChatSession
    await mixin._cancel_active_chat(conversation_id)
    await session.stop()
    mixin._chat_sessions.pop(conversation_id, None)
    if hasattr(mixin, "_session_create_locks"):
        mixin._session_create_locks.pop(conversation_id, None)

    # Unload voice models if no sessions remain
    if hasattr(mixin, "_check_voice_idle"):
        mixin._check_voice_idle()

    # Notify frontend
    await websocket.send(json.dumps({"type": "chat_cleared", "conversation_id": conversation_id}))
    logger.info(f"Chat cleared for conversation {conversation_id[:8]}")


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
        mixin._chat_sessions.pop(conversation_id, None)
        if hasattr(mixin, "_session_create_locks"):
            mixin._session_create_locks.pop(conversation_id, None)

    # Unload voice models if no sessions remain
    if hasattr(mixin, "_check_voice_idle"):
        mixin._check_voice_idle()

    # Soft-delete: mark as expired (preserves messages;
    # hard delete fails due to FK constraints from agent_runs, tasks, etc.)
    # Use 'expired' not 'handoff_ready' — no child session will pick these up.
    if db_session_id:
        session_manager = getattr(mixin, "session_manager", None)
        try:
            if session_manager:
                await asyncio.to_thread(session_manager.update, db_session_id, status="expired")
        except Exception as e:
            logger.warning(f"Failed to soft-delete session from DB: {e}")

    # Notify frontend
    await websocket.send(json.dumps({"type": "chat_deleted", "conversation_id": conversation_id}))
    logger.info(f"Chat deleted for conversation {conversation_id[:8]}")


async def cleanup_idle_sessions(mixin: SessionControlMixin) -> None:
    """Periodically disconnect chat sessions that have been idle too long."""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            now = datetime.now(UTC)
            stale_ids = [
                conv_id
                for conv_id, session in mixin._chat_sessions.items()
                if (now - session.last_activity).total_seconds() > IDLE_TIMEOUT_SECONDS
            ]
            for conv_id in stale_ids:
                # Fire SESSION_END before teardown (needs session in dict for lookup)
                await mixin._fire_session_end(conv_id)
                await mixin._cancel_active_chat(conv_id)
                session = mixin._chat_sessions.pop(conv_id, None)
                if hasattr(mixin, "_session_create_locks"):
                    mixin._session_create_locks.pop(conv_id, None)
                if session is None:
                    continue
                # Mark as paused in database before stopping
                if session.db_session_id:
                    session_manager = getattr(mixin, "session_manager", None)
                    if session_manager:
                        try:
                            await asyncio.to_thread(
                                session_manager.update, session.db_session_id, status="paused"
                            )
                        except Exception as e:
                            logger.warning(f"Failed to update session status: {e}")
                await session.stop()
                logger.debug(f"Cleaned up idle chat session {conv_id}")
            if stale_ids:
                logger.info(f"Cleaned up {len(stale_ids)} idle chat session(s)")
                # Unload voice models if no sessions remain
                if hasattr(mixin, "_check_voice_idle"):
                    mixin._check_voice_idle()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error in idle session cleanup")
