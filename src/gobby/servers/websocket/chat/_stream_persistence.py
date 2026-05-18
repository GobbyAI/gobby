"""Persistence helpers for chat response streaming."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from gobby.servers.websocket.chat.content_blocks import AssistantContentBlocks
from gobby.servers.websocket.chat_attachments import PreparedMessageAttachments
from gobby.servers.websocket.db import run_db

logger = logging.getLogger("gobby.servers.websocket.chat._messaging")


class ChatStreamPersistence:
    """Database persistence operations for a single chat stream."""

    def __init__(
        self,
        owner: Any,
        conversation_id: str,
        assistant_blocks: AssistantContentBlocks,
    ) -> None:
        self.owner = owner
        self.conversation_id = conversation_id
        self.assistant_blocks = assistant_blocks

    def session_ref(self) -> str | None:
        """Get the session ref (#N) for the current conversation."""
        session = self.owner._chat_sessions.get(self.conversation_id)
        if session and getattr(session, "seq_num", None):
            return f"#{session.seq_num}"
        return None

    async def persist_message(
        self,
        session: Any,
        role: str,
        text: str,
        content_blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        """Persist a chat message to the chat_messages table for display recovery."""
        try:
            from gobby.storage import chat_messages as cm_store

            session_manager = getattr(self.owner, "session_manager", None)
            if session_manager and session_manager.db:
                chat_session_id = getattr(session, "db_session_id", None) or self.conversation_id
                blocks_json = json.dumps(content_blocks) if content_blocks else None
                await run_db(
                    self.owner,
                    cm_store.save_message,
                    session_manager.db,
                    conversation_id=chat_session_id,
                    role=role,
                    content=text,
                    content_blocks_json=blocks_json,
                )
        except Exception as exc:
            logger.debug(f"Failed to persist chat message: {exc}")

    async def persist_user_message(
        self,
        session: Any,
        content: str | list[dict[str, Any]],
        attachments: PreparedMessageAttachments | None,
    ) -> None:
        """Persist the user message and any attachment content blocks."""
        user_text = content if isinstance(content, str) else json.dumps(content)
        if isinstance(content, list):
            user_content_blocks = list(content)
        else:
            user_content_blocks = [{"type": "text", "content": content}] if content.strip() else []
        if attachments and attachments.records:
            user_content_blocks.extend(attachments.content_blocks)
        await self.persist_message(session, "user", user_text, user_content_blocks)

    async def persist_current_assistant(self, session: Any) -> None:
        """Persist and reset accumulated assistant content blocks."""
        if not self.assistant_blocks.has_content():
            return
        await self.persist_message(
            session,
            "assistant",
            self.assistant_blocks.visible_text,
            list(self.assistant_blocks.blocks),
        )
        self.assistant_blocks.reset()

    async def persist_model_switch(self, session: Any, model: str) -> None:
        """Persist a successful mid-conversation model switch."""
        db_sid = getattr(session, "db_session_id", None)
        session_manager = getattr(self.owner, "session_manager", None)
        if not (db_sid and session_manager):
            return
        try:
            await run_db(self.owner, session_manager.update_model, db_sid, model)
        except Exception:
            logger.debug("Failed to persist switched model", exc_info=True)

    async def set_status(self, session: Any, status: str) -> None:
        """Best-effort session status update."""
        db_sid = getattr(session, "db_session_id", None)
        session_manager = getattr(self.owner, "session_manager", None)
        if not (db_sid and session_manager):
            return
        try:
            await run_db(self.owner, session_manager.update, db_sid, status=status)
        except Exception:
            logger.debug("Failed to set session status to %s", status, exc_info=True)

    async def persist_sdk_session_id(self, session: Any, sdk_session_id: str | None) -> None:
        """Persist provider-native session identity for resume/transcript linkage."""
        if not sdk_session_id:
            return
        db_sid = getattr(session, "db_session_id", None)
        session_manager = getattr(self.owner, "session_manager", None)
        if not (db_sid and session_manager):
            return
        try:
            await run_db(self.owner, session_manager.update, db_sid, external_id=sdk_session_id)
        except Exception:
            logger.debug(
                f"Failed to update external_id to SDK session_id for {db_sid}",
                exc_info=True,
            )

    async def persist_done_metadata(self, session: Any, event: Any) -> None:
        """Persist usage, context window, model, and paused status from DoneEvent."""
        db_sid = getattr(session, "db_session_id", None)
        session_manager = getattr(self.owner, "session_manager", None)
        if not (db_sid and session_manager):
            return

        has_usage = event.total_input_tokens is not None or event.output_tokens is not None
        if has_usage:
            try:
                prev_output = getattr(session, "_accumulated_output_tokens", 0)
                new_output = prev_output + (event.output_tokens or 0)
                session._accumulated_output_tokens = new_output

                await run_db(
                    self.owner,
                    session_manager.update_usage,
                    db_sid,
                    input_tokens=event.total_input_tokens or 0,
                    output_tokens=new_output,
                    cache_creation_tokens=event.cache_creation_input_tokens or 0,
                    cache_read_tokens=event.cache_read_input_tokens or 0,
                    context_window=event.context_window,
                    model=getattr(session, "_last_model", None),
                )
            except Exception:
                logger.warning(f"Failed to persist usage for {db_sid}", exc_info=True)
        else:
            await self._persist_context_window_and_model(session, db_sid, event)

        await self.set_status(session, "paused")

    async def _persist_context_window_and_model(
        self,
        session: Any,
        db_sid: str,
        event: Any,
    ) -> None:
        """Persist non-token DoneEvent metadata when usage is absent."""
        session_manager = getattr(self.owner, "session_manager", None)
        if not session_manager:
            return
        try:
            updates: dict[str, Any] = {}
            if event.context_window is not None:
                updates["context_window"] = event.context_window
            last_model = getattr(session, "_last_model", None)
            if last_model:
                updates["model"] = last_model
            if updates:
                updates["updated_at"] = datetime.now(UTC).isoformat()
                await run_db(
                    self.owner,
                    session_manager.db.safe_update,
                    "sessions",
                    updates,
                    "id = ?",
                    (db_sid,),
                )
        except Exception:
            logger.debug(f"Failed to persist context_window for {db_sid}", exc_info=True)
