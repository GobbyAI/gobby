"""Response metadata enrichment for hook events.

EventEnricher copies session metadata, terminal context, and workflow context
from the hook event into the response for adapter injection.
Also checks for undelivered inter-session messages (web chat -> CLI) and
injects them into the response context for hook piggyback delivery.
Extracted from HookManager.handle() as part of the Strangler Fig decomposition.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent, HookEventType, HookResponse

if TYPE_CHECKING:
    from gobby.storage.inter_session_messages import InterSessionMessageManager

logger = logging.getLogger(__name__)

# Terminal context keys copied from event metadata to response metadata
TERMINAL_CONTEXT_KEYS = [
    "terminal_term_program",
    "terminal_tty",
    "terminal_parent_pid",
    "terminal_iterm_session_id",
    "terminal_term_session_id",
    "terminal_kitty_window_id",
    "terminal_tmux_pane",
    "terminal_vscode_terminal_id",
    "terminal_alacritty_socket",
]

# Hook events that fire frequently during execution — good piggyback candidates.
# BEFORE_AGENT ensures messages arrive at the start of every agent turn,
# not just during tool calls (critical for spawned agents that haven't
# made a tool call yet).
_PIGGYBACK_EVENTS = {
    HookEventType.AFTER_TOOL,
    HookEventType.BEFORE_TOOL,
    HookEventType.BEFORE_AGENT,
}


class EventEnricher:
    """Enriches hook responses with session metadata and context.

    Copies platform session ID, external ID, machine ID, project ID,
    terminal context, and workflow context from the event into the response.
    Tracks first-hook-per-session for token optimization.
    Injects undelivered inter-session messages for hook piggyback delivery.
    """

    def __init__(
        self,
        session_storage: Any,  # Avoid runtime import of LocalSessionManager
        injected_sessions: set[str],
        inter_session_msg_manager: InterSessionMessageManager | None = None,
    ):
        self._session_storage = session_storage
        self._injected_sessions = injected_sessions
        self._inter_session_msg_manager = inter_session_msg_manager

    def enrich(
        self,
        event: HookEvent,
        response: HookResponse,
        workflow_context: str | None = None,
    ) -> None:
        """Enrich response with session metadata and context.

        Copies session metadata from event to response for adapter injection.
        The adapter reads response.metadata to inject session info into agent context.

        Args:
            event: Source hook event with metadata
            response: Response to enrich (modified in place)
            workflow_context: Optional workflow context to merge into response
        """
        # Copy session metadata
        if event.metadata.get("_platform_session_id"):
            platform_session_id: str = event.metadata["_platform_session_id"]
            response.metadata["session_id"] = platform_session_id

            # Look up seq_num for session_ref (#N format)
            # Guard with try/except: during shutdown the DB may already be closed
            if self._session_storage:
                try:
                    session_obj = self._session_storage.get(platform_session_id)
                except Exception:
                    session_obj = None
                if session_obj and session_obj.seq_num:
                    response.metadata["session_ref"] = f"#{session_obj.seq_num}"

            # Track first hook per session for token optimization
            # Adapters use this flag to inject full metadata only on first hook
            session_key = f"{platform_session_id}:{event.source.value}"
            is_first = session_key not in self._injected_sessions
            if is_first:
                self._injected_sessions.add(session_key)
            response.metadata["_first_hook_for_session"] = is_first

        if event.session_id:  # external_id (e.g., Claude Code's session UUID)
            response.metadata["external_id"] = event.session_id
        if event.machine_id:
            response.metadata["machine_id"] = event.machine_id
        if event.project_id:
            response.metadata["project_id"] = event.project_id

        # Copy terminal context if present
        for key in TERMINAL_CONTEXT_KEYS:
            if event.metadata.get(key):
                response.metadata[key] = event.metadata[key]

        # Merge workflow context if present
        if workflow_context:
            if response.context:
                response.context = f"{response.context}\n\n{workflow_context}"
            else:
                response.context = workflow_context

        # Hook piggyback: inject undelivered inter-session messages
        # Only on high-frequency events to avoid checking on every hook
        if (
            self._inter_session_msg_manager
            and event.event_type in _PIGGYBACK_EVENTS
            and event.metadata.get("_platform_session_id")
        ):
            try:
                self._inject_pending_messages(event.metadata["_platform_session_id"], response)
            except Exception as e:
                logger.debug(f"Piggyback message injection failed: {e}")

    def _inject_pending_messages(self, platform_session_id: str, response: HookResponse) -> None:
        """Check for and inject undelivered messages into response context.

        Groups messages by type (P2P, web_chat, command_result) and adds
        sender attribution for P2P messages.
        """
        if not self._inter_session_msg_manager:
            return

        undelivered = self._inter_session_msg_manager.get_undelivered_messages(platform_session_id)
        if not undelivered:
            return

        # Group by message_type
        groups: dict[str, list[Any]] = {}
        for msg in undelivered:
            msg_type = getattr(msg, "message_type", "message") or "message"
            groups.setdefault(msg_type, []).append(msg)
            # Mark as delivered
            try:
                self._inter_session_msg_manager.mark_delivered(msg.id)
            except Exception:
                pass

        # Format each group
        sections: list[str] = []
        for msg_type, msgs in groups.items():
            header = self._group_header(msg_type)
            lines = [header]
            for msg in msgs:
                urgent = "[URGENT] " if getattr(msg, "priority", "normal") == "urgent" else ""
                sender = self._resolve_sender_label(getattr(msg, "from_session", None))
                lines.append(f"- {urgent}{sender}{msg.content}")
            sections.append("\n".join(lines))

        pending_context = "\n\n".join(sections)
        if response.context:
            response.context = f"{response.context}\n\n{pending_context}"
        else:
            response.context = pending_context

    @staticmethod
    def _group_header(message_type: str) -> str:
        """Return the context header for a message type group."""
        if message_type == "web_chat":
            return "[Pending messages from web chat user]:"
        if message_type == "command_result":
            return "[Pending command results]:"
        # Default: P2P messages (message_type == "message")
        return "[Pending P2P messages from other sessions]:"

    def _resolve_sender_label(self, from_session: str | None) -> str:
        """Resolve a session ID to a human-readable sender label.

        Returns 'Session #N: ' if seq_num lookup succeeds, falls back to
        truncated UUID, or empty string if no sender.
        """
        if not from_session:
            return ""
        if self._session_storage:
            try:
                session_obj = self._session_storage.get(from_session)
                if session_obj and session_obj.seq_num:
                    return f"Session #{session_obj.seq_num}: "
            except Exception:
                pass
        return f"Session {from_session[:8]}: "
