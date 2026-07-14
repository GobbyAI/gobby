"""Pending inter-session message injection for web chat."""

from __future__ import annotations

import logging

from gobby.hooks.events import HookEventType

logger = logging.getLogger(__name__)


class ChatPendingMessagesMixin:
    """Pending message piggyback helpers for ChatMixin."""

    def _inject_pending_messages(
        self,
        db_session_id: str,
        event_type: HookEventType,
        *,
        pending_message_ids: list[str] | None = None,
    ) -> str | None:
        """Check for and inject undelivered inter-session messages.

        Runs on BEFORE_TOOL, AFTER_TOOL, and BEFORE_AGENT to match the CLI
        path's EventEnricher piggyback behavior. BEFORE_AGENT ensures messages
        arrive at agent turn start, even before any tool calls.
        """
        piggyback_events = {
            HookEventType.BEFORE_TOOL,
            HookEventType.AFTER_TOOL,
            HookEventType.BEFORE_AGENT,
        }
        if event_type not in piggyback_events:
            return None

        inter_session_msg_manager = getattr(self, "inter_session_msg_manager", None)
        if not inter_session_msg_manager:
            return None

        try:
            undelivered = inter_session_msg_manager.get_undelivered_messages(db_session_id)
            if not undelivered:
                return None

            groups: dict[tuple[str, str], list[str]] = {}
            collected_message_ids: list[str] = []
            for msg in undelivered:
                msg_type = getattr(msg, "message_type", "message") or "message"
                header = self._message_group_header(msg_type)
                urgent = "[URGENT] " if getattr(msg, "priority", "normal") == "urgent" else ""
                sender = self._resolve_chat_sender(getattr(msg, "from_session", None))
                rendered = f"- {urgent}{sender}{msg.content}"
                groups.setdefault((msg_type, header), []).append(rendered)
                msg_id = getattr(msg, "id", None)
                if isinstance(msg_id, str):
                    collected_message_ids.append(msg_id)

            if not groups:
                return None

            sections: list[str] = []
            for (_msg_type, header), rendered_messages in groups.items():
                lines = [header]
                lines.extend(rendered_messages)
                sections.append("\n".join(lines))

            context = "\n\n".join(sections)
            if pending_message_ids is not None:
                pending_message_ids.extend(collected_message_ids)
            return context
        except Exception as exc:
            logger.debug("Inter-session message piggyback failed: %s", exc, exc_info=True)
            return None

    def _mark_pending_messages_delivered(self, message_ids: list[str], db_session_id: str) -> None:
        """Acknowledge messages after their context survives lifecycle processing."""
        inter_session_msg_manager = getattr(self, "inter_session_msg_manager", None)
        if not inter_session_msg_manager:
            return

        for message_id in message_ids:
            try:
                inter_session_msg_manager.mark_delivered(message_id, db_session_id)
            except Exception:
                logger.debug(
                    "Failed to mark inter-session message %s delivered",
                    message_id,
                    exc_info=True,
                )

    @staticmethod
    def _message_group_header(message_type: str) -> str:
        """Return the context header for a message type group."""
        if message_type == "web_chat":
            return "[Pending messages from web chat user]:"
        if message_type == "command_result":
            return "[Pending command results]:"
        return "[Pending P2P messages from other sessions]:"

    @staticmethod
    def _resolve_chat_sender(from_session: str | None) -> str:
        """Resolve sender label using truncated UUID (no session storage in chat path)."""
        if not from_session:
            return ""
        return f"Session {from_session[:8]}: "
