"""Interaction response handlers for web chat."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEventType

logger = logging.getLogger(__name__)


class ChatInteractionResponsesMixin:
    """Handle user responses to pending chat interactions."""

    clients: dict[Any, dict[str, Any]]
    _chat_sessions: dict[str, Any]

    if TYPE_CHECKING:

        async def _fire_lifecycle(
            self,
            conversation_id: str,
            event_type: HookEventType,
            data: dict[str, Any],
        ) -> dict[str, Any] | None: ...

    async def _handle_ask_user_response(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle ask_user_response message from the web UI."""
        conversation_id = data.get("conversation_id")
        tool_call_id = data.get("tool_call_id")
        answers = data.get("answers", {})

        if not isinstance(conversation_id, str) or not conversation_id:
            logger.warning("ask_user_response for unknown conversation: %s", conversation_id)
            return
        session = self._chat_sessions.get(conversation_id)
        if session is None:
            logger.warning("ask_user_response for unknown conversation: %s", conversation_id)
            return

        if not isinstance(tool_call_id, str) or not session.provide_answer(tool_call_id, answers):
            logger.warning("ask_user_response but no pending question for %s", conversation_id)
            return
        await self._fire_lifecycle(
            conversation_id,
            HookEventType.NOTIFICATION,
            {
                "tool_use_id": tool_call_id,
                "_gobby_wait_resolution": "resumed",
            },
        )

    async def _handle_tool_approval_response(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle tool_approval_response message from the web UI."""
        conversation_id = data.get("conversation_id")
        tool_call_id = data.get("tool_call_id")
        decision = data.get("decision", "reject")
        if decision not in ("approve", "reject", "approve_always"):
            decision = "reject"

        if not isinstance(conversation_id, str) or not conversation_id:
            logger.warning("tool_approval_response for unknown conversation: %s", conversation_id)
            return
        session = self._chat_sessions.get(conversation_id)
        if session is None:
            logger.warning("tool_approval_response for unknown conversation: %s", conversation_id)
            return

        if isinstance(tool_call_id, str) and session.provide_approval(tool_call_id, decision):
            await self._fire_lifecycle(
                conversation_id,
                HookEventType.NOTIFICATION,
                {
                    "tool_use_id": tool_call_id,
                    "decision": decision,
                    "_gobby_wait_resolution": "resumed",
                },
            )
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
            logger.warning("tool_approval_response but no pending approval for %s", conversation_id)
            return

        logger.warning("tool_approval_response did not match a pending approval: %s", tool_call_id)

    async def _handle_heartbeat(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle heartbeat from web UI to keep session alive during idle periods."""
        conversation_id = data.get("conversation_id")
        session = self._chat_sessions.get(conversation_id) if conversation_id else None
        if session:
            client_info = self.clients.get(websocket)
            if client_info is not None:
                client_info["conversation_id"] = conversation_id
                client_info["project_id"] = getattr(session, "project_id", None)
            session.last_activity = datetime.now(UTC)
            logger.debug(
                "Heartbeat received for conversation %s",
                conversation_id[:8] if conversation_id else "?",
            )
