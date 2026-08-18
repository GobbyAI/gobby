"""Chat message routes for web chat display persistence."""

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query

from gobby.servers.chat_attachment_cleanup import cleanup_conversation_attachments
from gobby.storage import chat_messages

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


def create_chat_router(server: "HTTPServer") -> APIRouter:
    """Create chat router with message persistence endpoints."""
    router = APIRouter(prefix="/api/chat", tags=["chat"])

    def _get_db() -> Any:
        if server.session_manager is None:
            raise HTTPException(status_code=503, detail="Session manager not available")
        return server.session_manager.db

    async def _delete_attachment_files_for_conversations(
        db: Any,
        conversation_ids: list[str],
    ) -> None:
        for conversation_id in conversation_ids:
            await cleanup_conversation_attachments(
                db,
                conversation_id,
                run_db=server.run_db,
                terminal=True,
            )

    @router.get("/{conversation_id}/messages")
    async def get_messages(
        conversation_id: str,
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=1000),
    ) -> dict[str, Any]:
        """Load chat messages for a conversation."""
        db = _get_db()
        messages = await server.run_db(
            chat_messages.get_messages,
            db,
            conversation_id,
            after_seq=after_seq,
            limit=limit,
        )
        max_seq = await server.run_db(chat_messages.get_max_seq, db, conversation_id)
        return {"messages": messages, "max_seq": max_seq}

    @router.delete("/{conversation_id}/messages")
    async def delete_messages(conversation_id: str) -> dict[str, Any]:
        """Delete all chat messages for a conversation."""
        db = _get_db()
        await _delete_attachment_files_for_conversations(db, [conversation_id])
        count = await server.run_db(chat_messages.delete_messages, db, conversation_id)
        return {"deleted": count}

    return router
