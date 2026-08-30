"""Response metadata enrichment for hook events.

EventEnricher copies session metadata, terminal context, and workflow context
from the hook event into the response for adapter injection.
It also injects undelivered inter-session messages into provider hooks that
carry model context.
Extracted from HookManager.handle() as part of the Strangler Fig decomposition.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.adapters.capabilities import ContextChannel, get_provider_capabilities
from gobby.hooks import grok_pending_context
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.pending_messages import PendingMessageRenderResult, render_pending_messages
from gobby.hooks.receipt_effects import STAGED_EFFECTS_FIELD, record_worker_staging

# Only inject full session metadata (IDs, terminal context) on context-building
# events, never on lifecycle events like Stop or per-tool events.
_METADATA_INJECTION_EVENTS = {
    HookEventType.SESSION_START,
    HookEventType.BEFORE_AGENT,
}

if TYPE_CHECKING:
    from gobby.storage.inter_session_messages import InterSessionMessageManager

logger = logging.getLogger(__name__)

# Terminal context keys copied from event metadata to response metadata
TERMINAL_CONTEXT_KEYS = [
    "terminal_term_program",
    "terminal_tty",
    "terminal_parent_pid",
    "terminal_tmux_pane",
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
    Injects memory recall references and inter-session messages.
    """

    def __init__(
        self,
        session_manager: Any,  # Avoid runtime import of SessionManager
        injected_sessions: set[str],
        inter_session_msg_manager: InterSessionMessageManager | None = None,
    ):
        self._session_manager = session_manager
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
            if self._session_manager:
                try:
                    session_obj = self._session_manager.get(platform_session_id)
                except Exception:
                    session_obj = None
                if session_obj and session_obj.seq_num:
                    response.metadata["session_ref"] = f"#{session_obj.seq_num}"

            # Track first hook per session for token optimization
            # Adapters use this flag to inject full metadata only on first hook.
            # Only allow metadata injection on context-building events
            # (SESSION_START, BEFORE_AGENT) — never on Stop, PreToolUse, etc.
            session_key = f"{platform_session_id}:{event.source.value}"
            is_first = session_key not in self._injected_sessions
            is_eligible = event.event_type in _METADATA_INJECTION_EVENTS
            if is_first and is_eligible:
                self._injected_sessions.add(session_key)
            response.metadata["_first_hook_for_session"] = is_first and is_eligible

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

        raw_delivery_session_id = event.metadata.get("_platform_session_id")
        delivery_session_id = (
            raw_delivery_session_id
            if isinstance(raw_delivery_session_id, str) and raw_delivery_session_id
            else None
        )
        supports_context = self._hook_supports_context(event)
        # Hook piggyback: inject undelivered inter-session messages.
        if (
            self._inter_session_msg_manager
            and event.event_type in _PIGGYBACK_EVENTS
            and delivery_session_id
            and (supports_context or event.source == SessionSource.GROK)
        ):
            try:
                self._inject_pending_messages(event, delivery_session_id, response)
            except Exception as e:
                logger.debug("Piggyback message injection failed: %s", e)

        if event.event_type == HookEventType.SESSION_END and event.metadata.get(
            "_platform_session_id"
        ):
            session_key = f"{event.metadata['_platform_session_id']}:{event.source.value}"
            self._injected_sessions.discard(session_key)

    def _inject_pending_messages(
        self,
        event: HookEvent,
        platform_session_id: str,
        response: HookResponse,
    ) -> None:
        """Check for and inject undelivered messages into response context.

        Groups messages by type (P2P, web_chat, command_result) and adds
        sender attribution for P2P messages.
        """
        if not self._inter_session_msg_manager:
            return

        undelivered = self._inter_session_msg_manager.get_undelivered_messages(platform_session_id)
        if not undelivered:
            return

        rendered = render_pending_messages(
            undelivered,
            resolve_sender=self._resolve_sender_label,
        )
        if event.source == SessionSource.GROK:
            grok_pending_context.enqueue_pending_messages(
                self._session_manager,
                platform_session_id,
                undelivered,
                self._resolve_sender_label,
            )
            self._stage_pending_messages(response, rendered, platform_session_id)
            return

        pending_context = rendered.context
        if not pending_context:
            return
        if response.context:
            response.context = f"{pending_context}\n\n{response.context}"
        else:
            response.context = pending_context
        self._stage_pending_messages(response, rendered, platform_session_id)

    @staticmethod
    def _stage_pending_messages(
        response: HookResponse,
        rendered: PendingMessageRenderResult,
        platform_session_id: str,
    ) -> None:
        if not rendered.represented_message_ids:
            return
        staged = {
            "pending_message_ids": list(rendered.represented_message_ids),
            "pending_message_session_id": platform_session_id,
        }
        response.metadata[STAGED_EFFECTS_FIELD] = staged
        record_worker_staging(staged)

    @staticmethod
    def _hook_supports_context(event: HookEvent) -> bool:
        """Return whether this exact native provider hook carries model context."""
        native_hook_type = event.metadata.get("_native_hook_type")
        if not isinstance(native_hook_type, str) or not native_hook_type:
            return False
        try:
            capability = get_provider_capabilities(event.source).get_hook(native_hook_type)
        except ValueError:
            return False
        return bool(
            capability
            and capability.event_type is event.event_type
            and capability.context_channel is not ContextChannel.NONE
        )

    def _resolve_sender_label(self, from_session: str | None) -> str:
        """Resolve a session ID to a human-readable sender label.

        Returns 'Session #N: ' if seq_num lookup succeeds, falls back to
        truncated UUID, or empty string if no sender.
        """
        if not from_session:
            return ""
        if self._session_manager:
            try:
                session_obj = self._session_manager.get(from_session)
                if session_obj and session_obj.seq_num:
                    return f"Session #{session_obj.seq_num}: "
            except Exception:
                pass
        return f"Session {from_session[:8]}: "
