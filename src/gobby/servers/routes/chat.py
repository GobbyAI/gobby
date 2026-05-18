"""Chat message routes for web chat display persistence."""

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query

from gobby.servers.chat_attachment_files import unlink_stored_attachment_file
from gobby.storage import chat_attachments, chat_messages

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def create_chat_router(server: "HTTPServer") -> APIRouter:
    """Create chat router with message persistence endpoints."""
    router = APIRouter(prefix="/api/chat", tags=["chat"])

    def _get_db() -> Any:
        if server.session_manager is None:
            raise HTTPException(status_code=503, detail="Session manager not available")
        return server.session_manager.db

    def _resolve_chat_message_keys(chat_id: str) -> tuple[str, str | None]:
        """Resolve primary/fallback keys for web-chat display persistence.

        New web-chat messages are keyed by DB session ID. Older rows may still
        be keyed by the session's former external_id, so we transparently fall
        back for read/delete compatibility during the transition.
        """
        if server.session_manager is None:
            return chat_id, None

        try:
            session = server.session_manager.get(chat_id)
        except Exception as exc:
            logger.debug(
                "Chat message key resolution failed for %s: %s", chat_id, exc, exc_info=True
            )
            session = None

        if not session or getattr(session, "session_type", None) != "web_chat":
            return chat_id, None

        fallback = session.external_id if session.external_id != session.id else None
        return session.id, fallback

    async def _delete_attachment_files_for_conversations(
        db: Any,
        conversation_ids: list[str],
    ) -> None:
        records = chat_attachments.delete_attachments_for_conversations(db, conversation_ids)
        for record in records:
            await unlink_stored_attachment_file(record.local_path, record_id=record.id)

    @router.get("/{conversation_id}/messages")
    async def get_messages(
        conversation_id: str,
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=1000),
    ) -> dict[str, Any]:
        """Load chat messages for a conversation."""
        db = _get_db()
        primary_key, fallback_key = _resolve_chat_message_keys(conversation_id)

        messages = chat_messages.get_messages(db, primary_key, after_seq=after_seq, limit=limit)
        max_seq = chat_messages.get_max_seq(db, primary_key)

        if not messages and fallback_key:
            messages = chat_messages.get_messages(
                db, fallback_key, after_seq=after_seq, limit=limit
            )
            max_seq = chat_messages.get_max_seq(db, fallback_key)
        return {"messages": messages, "max_seq": max_seq}

    @router.delete("/{conversation_id}/messages")
    async def delete_messages(conversation_id: str) -> dict[str, Any]:
        """Delete all chat messages for a conversation."""
        db = _get_db()
        primary_key, fallback_key = _resolve_chat_message_keys(conversation_id)
        cleanup_keys = [primary_key]
        if fallback_key:
            cleanup_keys.append(fallback_key)
        await _delete_attachment_files_for_conversations(db, cleanup_keys)
        count = chat_messages.delete_messages(db, primary_key)
        if fallback_key:
            count += chat_messages.delete_messages(db, fallback_key)
        return {"deleted": count}

    return router
