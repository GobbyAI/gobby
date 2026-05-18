"""Interaction response handlers for web chat."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("gobby.servers.websocket.chat._messaging")


class ChatInteractionResponsesMixin:
    """Handle user responses to pending chat interactions."""

    _chat_sessions: dict[str, Any]

    async def _handle_ask_user_response(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle ask_user_response message from the web UI."""
        conversation_id = data.get("conversation_id")
        answers = data.get("answers", {})

        session = self._chat_sessions.get(conversation_id) if conversation_id else None
        if session is None:
            logger.warning(f"ask_user_response for unknown conversation: {conversation_id}")
            return

        if not session.has_pending_question:
            logger.warning(f"ask_user_response but no pending question for {conversation_id}")
            return

        session.provide_answer(answers)

    async def _handle_tool_approval_response(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle tool_approval_response message from the web UI."""
        conversation_id = data.get("conversation_id")
        tool_call_id = data.get("tool_call_id")
        decision = data.get("decision", "reject")
        if decision not in ("approve", "reject", "approve_always"):
            decision = "reject"

        session = self._chat_sessions.get(conversation_id) if conversation_id else None
        if session is None:
            logger.warning(f"tool_approval_response for unknown conversation: {conversation_id}")
            return

        if not session.has_pending_approval:
            manager = getattr(self, "_pending_interaction_manager", None)
            if manager and isinstance(tool_call_id, str) and tool_call_id:
                try:
                    resolved = await manager.resolve(tool_call_id, decision)
                except Exception:
                    logger.exception(
                        "Failed to resolve pending interaction",
                        extra={
                            "tool_call_id": tool_call_id,
                            "conversation_id": conversation_id,
                        },
                    )
                    resolved = False
                if resolved:
                    return
            logger.warning(f"tool_approval_response but no pending approval for {conversation_id}")
            return

        session.provide_approval(decision)

    async def _handle_heartbeat(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle heartbeat from web UI to keep session alive during idle periods."""
        conversation_id = data.get("conversation_id")
        session = self._chat_sessions.get(conversation_id) if conversation_id else None
        if session:
            session.last_activity = datetime.now(UTC)
            logger.debug(
                f"Heartbeat received for conversation {conversation_id[:8] if conversation_id else '?'}"
            )
