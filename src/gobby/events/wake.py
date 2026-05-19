"""Wake dispatcher for notifying sessions when async operations complete.

Routes wake messages based on session type after first persisting a durable
InterSessionMessage:
- Terminal agents (agent_depth > 0, terminal_context): tmux send-keys wake signal
- SDK agents (agent_depth > 0, sdk_session_id): SDK resume wake signal
- Interactive sessions (agent_depth 0): tmux pane wake signal when available
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, Protocol, cast

from gobby.sessions.tmux_context import get_tmux_socket_path, parse_terminal_context_value

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.inter_session_messages import InterSessionMessageManager
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

CONTINUE_WAKE_SIGNAL = "Message from Gobby daemon: Job's Done.\n"

# Coalesce bursty completions targeting an interactive pane: while the user is
# idle on the same turn, suppress redundant tmux send-keys after the first wake.
# The 30s ceiling guarantees we resume nudging if turn_count signals get missed.
PANE_WAKE_DEBOUNCE_SECONDS = 30.0

# tmux_sender signature: (tmux_session_name: str, message: str) -> None
TmuxSender = Callable[[str, str], Coroutine[Any, Any, None]]

# tmux_pane_sender signature: (pane_id: str, message: str, socket_path: str | None) -> None
TmuxPaneSender = Callable[[str, str, str | None], Coroutine[Any, Any, None]]

# sdk_resumer signature: (sdk_session_id: str, message: str) -> None
SdkResumer = Callable[[str, str], Coroutine[Any, Any, None]]


class WebChatSessionRegistryProtocol(Protocol):
    async def wake_session(self, session_id: str) -> dict[str, Any]: ...


class WakeDispatcher:
    """Dispatches wake messages to sessions based on their type.

    Constructor args:
        session_manager: For looking up session metadata (agent_depth, terminal_context)
        ism_manager: For creating InterSessionMessages (durable fallback)
        tmux_sender: Optional async callable to send keys to a tmux session
        sdk_resumer: Optional async callable to resume an SDK session with a new prompt
        agent_run_manager: Optional manager for looking up sdk_session_id from agent runs
    """

    def __init__(
        self,
        session_manager: SessionManager,
        ism_manager: InterSessionMessageManager,
        tmux_sender: TmuxSender | None = None,
        tmux_pane_sender: TmuxPaneSender | None = None,
        sdk_resumer: SdkResumer | None = None,
        agent_run_manager: LocalAgentRunManager | None = None,
        web_chat_session_registry: WebChatSessionRegistryProtocol | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._ism_manager = ism_manager
        self._tmux_sender = tmux_sender
        self._tmux_pane_sender = tmux_pane_sender
        self._sdk_resumer = sdk_resumer
        self._agent_run_manager = agent_run_manager
        self._web_chat_session_registry = web_chat_session_registry
        # session_id -> (turn_count_at_last_wake, monotonic_ts_at_last_wake)
        self._last_pane_wake: dict[str, tuple[int, float]] = {}

    def set_web_chat_session_registry(
        self,
        registry: WebChatSessionRegistryProtocol | None,
    ) -> None:
        """Wire the live web-chat registry after server initialization."""
        self._web_chat_session_registry = registry

    async def wake(
        self,
        session_id: str,
        message: str,
        result: dict[str, Any],
    ) -> None:
        """Wake a session with a completion notification.

        Args:
            session_id: Target session to wake
            message: Human-readable notification message
            result: Structured result data
        """
        session = self._session_manager.get(session_id)
        if session is None:
            logger.warning(f"Cannot wake session {session_id}: not found")
            return

        if not self._send_ism(session_id, message, result):
            return

        await self.dispatch_live_wake(session_id, session=session)

    async def dispatch_live_wake(
        self,
        session_id: str,
        *,
        session: Any | None = None,
    ) -> dict[str, Any]:
        """Send a live wake signal after durable mailbox storage is complete."""
        session = session or self._session_manager.get(session_id)
        if session is None:
            logger.warning(f"Cannot wake session {session_id}: not found")
            return {
                "session_id": session_id,
                "delivered": False,
                "method": None,
                "error": "session_not_found",
            }

        agent_depth = getattr(session, "agent_depth", 0) or 0
        terminal_context = getattr(session, "terminal_context", None)
        session_type = getattr(session, "session_type", None)

        if session_type == "web_chat":
            return await self._dispatch_web_chat_wake(session_id)

        # Interactive session → try tmux pane wake after durable message storage.
        if agent_depth == 0:
            if terminal_context and self._tmux_pane_sender:
                tmux_pane = self._parse_tmux_pane(terminal_context)
                if tmux_pane and self._should_send_pane_wake(session_id, session):
                    tmux_socket_path = self._parse_tmux_socket_path(terminal_context)
                    try:
                        await self._tmux_pane_sender(
                            tmux_pane,
                            CONTINUE_WAKE_SIGNAL,
                            tmux_socket_path,
                        )
                        self._record_pane_wake(session_id, session)
                        return {
                            "session_id": session_id,
                            "delivered": True,
                            "method": "tmux_pane",
                        }
                    except Exception:
                        logger.warning(
                            f"tmux pane wake failed for session {session_id} (pane={tmux_pane})",
                            exc_info=True,
                        )
                        return {
                            "session_id": session_id,
                            "delivered": False,
                            "method": "tmux_pane",
                            "error": "tmux_pane_wake_failed",
                        }
            return {"session_id": session_id, "delivered": False, "method": None}

        # Terminal agent → try tmux, then SDK. Both are wake signals only.
        if terminal_context and self._tmux_sender:
            tmux_session_name = self._parse_tmux_session(terminal_context)
            if tmux_session_name:
                try:
                    await self._tmux_sender(tmux_session_name, CONTINUE_WAKE_SIGNAL)
                    return {
                        "session_id": session_id,
                        "delivered": True,
                        "method": "tmux",
                    }
                except Exception:
                    logger.warning(
                        f"tmux wake failed for session {session_id} (tmux={tmux_session_name}), trying SDK resume",
                        exc_info=True,
                    )

        # SDK agent → try resume via sdk_session_id
        if self._sdk_resumer:
            sdk_session_id = self._resolve_sdk_session_id(session_id)
            if sdk_session_id:
                try:
                    await self._sdk_resumer(sdk_session_id, CONTINUE_WAKE_SIGNAL)
                    return {
                        "session_id": session_id,
                        "delivered": True,
                        "method": "sdk",
                    }
                except Exception:
                    logger.warning(
                        f"SDK resume failed for session {session_id} (sdk={sdk_session_id})",
                        exc_info=True,
                    )
                    return {
                        "session_id": session_id,
                        "delivered": False,
                        "method": "sdk",
                        "error": "sdk_resume_failed",
                    }

        return {"session_id": session_id, "delivered": False, "method": None}

    async def _dispatch_web_chat_wake(self, session_id: str) -> dict[str, Any]:
        if self._web_chat_session_registry is None:
            return self._web_chat_no_live_result(session_id)

        try:
            result = await self._web_chat_session_registry.wake_session(session_id)
        except Exception as exc:
            logger.warning(
                "web_chat wake failed for session %s: %s",
                session_id,
                exc,
                exc_info=True,
            )
            return {
                "session_id": session_id,
                "delivered": False,
                "method": "web_chat",
                "error": str(exc),
                "error_code": "web_chat_wake_failed",
                "error_message": str(exc),
            }

        if not isinstance(result, dict):
            return {
                "session_id": session_id,
                "delivered": False,
                "method": "web_chat",
                "error_code": "web_chat_wake_failed",
            }
        result.setdefault("session_id", session_id)
        result.setdefault("method", "web_chat")
        return result

    @staticmethod
    def _web_chat_no_live_result(session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "delivered": False,
            "method": "web_chat",
            "error": "no_live_web_chat_session",
            "error_code": "no_live_web_chat_session",
            "error_message": f"No live web_chat session found for {session_id}",
        }

    def _should_send_pane_wake(self, session_id: str, session: Any) -> bool:
        """Decide whether to send a live tmux pane wake to an interactive session.

        Coalesces bursty completions: if a pane wake was already delivered to
        this session and the user has not advanced the turn since (and the 30s
        ceiling has not elapsed), skip the live nudge. Durable ISMs are stored
        unconditionally, so the agent still sees every completion when it next
        reads its inbox.
        """
        now = time.monotonic()
        stale_before = now - PANE_WAKE_DEBOUNCE_SECONDS
        for recorded_session_id, (_, recorded_ts) in tuple(self._last_pane_wake.items()):
            if recorded_ts < stale_before:
                self._last_pane_wake.pop(recorded_session_id, None)

        last = self._last_pane_wake.get(session_id)
        if last is None:
            return True
        last_turn, last_ts = last
        current_turn = int(getattr(session, "turn_count", 0) or 0)
        if current_turn > last_turn:
            return True
        return (now - last_ts) >= PANE_WAKE_DEBOUNCE_SECONDS

    def _record_pane_wake(self, session_id: str, session: Any) -> None:
        """Record that a tmux pane wake was just delivered to this session."""
        current_turn = int(getattr(session, "turn_count", 0) or 0)
        self._last_pane_wake[session_id] = (current_turn, time.monotonic())

    def _resolve_sdk_session_id(self, session_id: str) -> str | None:
        """Look up the SDK session ID for a session via agent_runs.

        Checks if the session is a child of an agent run that captured
        an sdk_session_id during execution.
        """
        if not self._agent_run_manager:
            return None
        try:
            # Check if session itself has an external_id (SDK session)
            session = self._session_manager.get(session_id)
            if session and getattr(session, "external_id", None):
                return cast(str | None, session.external_id)

            # Check agent_runs where this session is the child
            sdk_id = self._agent_run_manager.get_sdk_session_id_for_session(session_id)
            return sdk_id
        except Exception:
            logger.debug(
                f"Could not resolve sdk_session_id for session {session_id}",
                exc_info=True,
            )
            return None

    def _send_ism(self, session_id: str, message: str, result: dict[str, Any]) -> bool:
        """Send an InterSessionMessage as durable notification."""
        try:
            content = str(
                result.get("signoff_message")
                or result.get("continuation_prompt")
                or message
                or "Completion available"
            )
            from_session = str(result.get("from_session_id") or session_id)
            message_type = str(result.get("message_type") or "completion_notification")
            metadata = {**result, "completion_message": message}
            completion_id = self._notification_completion_id(metadata)
            if completion_id and "completion_id" not in metadata:
                metadata["completion_id"] = completion_id
            if completion_id and self._notification_exists(session_id, message_type, completion_id):
                return True
            self._ism_manager.create_message(
                from_session=from_session,
                to_session=session_id,
                content=content,
                message_type=message_type,
                priority="high",
                metadata_json=json.dumps(metadata, default=str, sort_keys=True),
            )
            return True
        except Exception:
            logger.error(
                f"Failed to send ISM to session {session_id}",
                exc_info=True,
            )
            return False

    def _notification_exists(
        self,
        session_id: str,
        message_type: str,
        completion_id: str,
    ) -> bool:
        """Return True if this durable completion notification already exists."""
        has_notification = getattr(
            type(self._ism_manager),
            "has_completion_notification",
            None,
        )
        if callable(has_notification):
            try:
                return bool(
                    has_notification(self._ism_manager, session_id, message_type, completion_id)
                )
            except Exception:
                logger.debug(
                    f"Could not query existing completion notification for {session_id}",
                    exc_info=True,
                )

        list_messages = getattr(self._ism_manager, "list_messages", None)
        if not callable(list_messages):
            return False
        try:
            messages = list_messages(
                session_id,
                direction="inbox",
                message_type=message_type,
                limit=100,
            )
        except Exception:
            logger.debug(
                f"Could not query existing completion notifications for {session_id}",
                exc_info=True,
            )
            return False

        for msg in messages:
            metadata_json = getattr(msg, "metadata_json", None)
            if not metadata_json:
                continue
            try:
                metadata = json.loads(metadata_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if self._notification_completion_id(metadata) == completion_id:
                return True
        return False

    @staticmethod
    def _notification_completion_id(metadata: dict[str, Any]) -> str | None:
        """Resolve the stable id used to dedupe completion notifications."""
        value = (
            metadata.get("completion_id") or metadata.get("run_id") or metadata.get("execution_id")
        )
        return str(value) if value else None

    @staticmethod
    def _parse_tmux_session(terminal_context: Any) -> str | None:
        """Extract tmux session name from terminal_context JSON."""
        ctx = parse_terminal_context_value(terminal_context)
        if not ctx:
            return None
        value = ctx.get("tmux_session")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _parse_tmux_pane(terminal_context: Any) -> str | None:
        """Extract tmux pane ID from terminal_context JSON."""
        ctx = parse_terminal_context_value(terminal_context)
        if not ctx:
            return None
        value = ctx.get("tmux_pane")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _parse_tmux_socket_path(terminal_context: Any) -> str | None:
        """Extract tmux socket path from terminal_context JSON."""
        ctx = parse_terminal_context_value(terminal_context)
        if not ctx:
            return None
        return get_tmux_socket_path(ctx)
