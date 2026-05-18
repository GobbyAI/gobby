"""Pending inter-session message injection for web chat."""

from __future__ import annotations

import logging
from typing import Any

from gobby.hooks.events import HookEventType

logger = logging.getLogger("gobby.servers.websocket.chat._messaging")


class ChatPendingMessagesMixin:
    """Pending message piggyback helpers for ChatMixin."""

    def _inject_pending_messages(
        self,
        db_session_id: str,
        event_type: HookEventType,
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

            groups: dict[str, list[Any]] = {}
            for msg in undelivered:
                msg_type = getattr(msg, "message_type", "message") or "message"
                groups.setdefault(msg_type, []).append(msg)
                try:
                    inter_session_msg_manager.mark_delivered(msg.id)
                except Exception:
                    pass

            sections: list[str] = []
            for msg_type, msgs in groups.items():
                header = self._message_group_header(msg_type)
                lines = [header]
                for msg in msgs:
                    urgent = "[URGENT] " if getattr(msg, "priority", "normal") == "urgent" else ""
                    sender = self._resolve_chat_sender(getattr(msg, "from_session", None))
                    lines.append(f"- {urgent}{sender}{msg.content}")
                sections.append("\n".join(lines))

            return "\n\n".join(sections)
        except Exception as exc:
            logger.debug(f"Inter-session message piggyback failed: {exc}")
            return None

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
