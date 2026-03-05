"""Wake dispatcher for notifying sessions when async operations complete.

Routes wake messages based on session type:
- Terminal agents (agent_depth > 0, terminal_context): tmux send-keys
- SDK agents (agent_depth > 0, sdk_session_id): resume via SDK
- Interactive sessions (agent_depth 0): InterSessionMessage
- Fallback: ISM if tmux/SDK fails
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.inter_session_messages import InterSessionMessageManager
    from gobby.storage.sessions import LocalSessionManager

logger = logging.getLogger(__name__)

# tmux_sender signature: (tmux_session_name: str, message: str) -> None
TmuxSender = Callable[[str, str], Coroutine[Any, Any, None]]

# sdk_resumer signature: (sdk_session_id: str, message: str) -> None
SdkResumer = Callable[[str, str], Coroutine[Any, Any, None]]


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
        session_manager: LocalSessionManager,
        ism_manager: InterSessionMessageManager,
        tmux_sender: TmuxSender | None = None,
        sdk_resumer: SdkResumer | None = None,
        agent_run_manager: LocalAgentRunManager | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._ism_manager = ism_manager
        self._tmux_sender = tmux_sender
        self._sdk_resumer = sdk_resumer
        self._agent_run_manager = agent_run_manager

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
            logger.warning("Cannot wake session %s: not found", session_id)
            return

        agent_depth = getattr(session, "agent_depth", 0) or 0

        # Interactive session → always ISM
        if agent_depth == 0:
            self._send_ism(session_id, message)
            return

        # Terminal agent → try tmux, fall back to SDK, then ISM
        terminal_context = getattr(session, "terminal_context", None)
        if terminal_context and self._tmux_sender:
            tmux_session_name = self._parse_tmux_session(terminal_context)
            if tmux_session_name:
                try:
                    await self._tmux_sender(tmux_session_name, message)
                    return
                except Exception:
                    logger.warning(
                        "tmux wake failed for session %s (tmux=%s), trying SDK resume",
                        session_id,
                        tmux_session_name,
                        exc_info=True,
                    )

        # SDK agent → try resume via sdk_session_id
        if self._sdk_resumer:
            sdk_session_id = self._resolve_sdk_session_id(session_id)
            if sdk_session_id:
                try:
                    await self._sdk_resumer(sdk_session_id, message)
                    return
                except Exception:
                    logger.warning(
                        "SDK resume failed for session %s (sdk=%s), falling back to ISM",
                        session_id,
                        sdk_session_id,
                        exc_info=True,
                    )

        # Fallback: ISM
        self._send_ism(session_id, message)

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
                "Could not resolve sdk_session_id for session %s",
                session_id,
                exc_info=True,
            )
            return None

    def _send_ism(self, session_id: str, message: str) -> None:
        """Send an InterSessionMessage as durable notification."""
        try:
            self._ism_manager.create_message(
                from_session=session_id,  # self-notification (system → session)
                to_session=session_id,
                content=message,
                message_type="completion_notification",
                priority="high",
            )
        except Exception:
            logger.error(
                "Failed to send ISM to session %s",
                session_id,
                exc_info=True,
            )

    @staticmethod
    def _parse_tmux_session(terminal_context: str) -> str | None:
        """Extract tmux session name from terminal_context JSON."""
        try:
            ctx = json.loads(terminal_context)
            return cast(str | None, ctx.get("tmux_session"))
        except (json.JSONDecodeError, TypeError):
            return None
