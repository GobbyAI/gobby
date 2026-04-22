"""CLI session liveness monitor.

Polls active sessions to detect when the parent CLI process (Claude Code,
Gemini CLI, etc.) has exited.  When the parent PID is dead the session is
expired and summary generation is dispatched while the JSONL transcript
file still exists on disk.

This is the fast-path counterpart to the 24-hour stale-session expiry in
SessionLifecycleManager, reducing the detection window from hours to
~30 seconds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from gobby.config.tmux import TmuxConfig

if TYPE_CHECKING:
    from gobby.sessions.processor import SessionMessageProcessor
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

# How long a session_id stays in the recently-handled set (seconds)
_RECENTLY_HANDLED_TTL = 120.0

# Default polling interval (seconds)
_DEFAULT_POLL_INTERVAL = 30.0


class SessionLivenessMonitor:
    """Background task that detects dead CLI sessions via parent PID checks.

    When the parent process that owns a session exits (e.g. user typed
    ``/exit``, process crashed, terminal closed), this monitor:

    1. Dispatches summary generation while the transcript file is still fresh.
    2. Marks the session as ``expired``.
    3. Unregisters the session from the message processor.

    Args:
        session_storage: Session manager for DB queries and status updates.
        dispatch_summaries_fn: Callback to generate session summaries.
            Signature: ``(session_id: str, background: bool, done_event) -> None``
        message_processor: Optional session message processor for cleanup.
        poll_interval: Seconds between polls (default 30).
    """

    def __init__(
        self,
        session_storage: SessionManager,
        dispatch_summaries_fn: Callable[..., None] | None = None,
        generate_summaries_fn: Callable[..., Coroutine[Any, Any, None]] | None = None,
        message_processor: SessionMessageProcessor | None = None,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._session_manager = session_storage
        self._dispatch_summaries_fn = dispatch_summaries_fn
        self._generate_summaries_fn = generate_summaries_fn
        self._message_processor = message_processor
        self._poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        # session_id -> monotonic timestamp when we handled it
        self._recently_handled: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background polling task."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._poll_loop(), name="session-liveness-monitor")
        logger.info(f"SessionLivenessMonitor started (interval={self._poll_interval:.0f}s)")

    async def stop(self) -> None:
        """Cancel the background polling task."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("SessionLivenessMonitor stopped")

    def mark_recently_handled(self, session_id: str) -> None:
        """Record that a session was just handled by another mechanism.

        Prevents duplicate processing when e.g. a normal ``session_end``
        hook fires shortly before the liveness check.
        """
        self._recently_handled[session_id] = time.monotonic()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Infinite loop: sleep, check sessions, repeat."""
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                await self._check_sessions()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("SessionLivenessMonitor poll error (continuing)")

    async def _check_sessions(self) -> None:
        """Check active sessions for dead parent PIDs."""
        # 1. Prune expired entries from recently-handled set
        now = time.monotonic()
        expired = [
            sid for sid, ts in self._recently_handled.items() if now - ts > _RECENTLY_HANDLED_TTL
        ]
        for sid in expired:
            del self._recently_handled[sid]

        # 2. Query active sessions with terminal_context
        active_sessions = self._get_active_sessions_with_pid()
        if not active_sessions:
            return

        # 3. Check each session's parent PID (and tmux pane if present)
        for session_id, parent_pid, tmux_pane, tmux_socket_path in active_sessions:
            if session_id in self._recently_handled:
                continue

            if self._is_pid_alive(parent_pid):
                continue

            # Parent PID is dead — but if running in tmux, the pane may
            # still be alive (e.g. terminal tab closed while tmux persists).
            if tmux_pane and self._is_tmux_pane_alive(tmux_pane, tmux_socket_path):
                logger.debug(
                    f"Session {session_id} parent PID {parent_pid} dead but tmux pane {tmux_pane} alive - refreshing",
                )
                try:
                    self._session_manager.touch(session_id)
                except Exception:
                    logger.warning(
                        f"SessionLivenessMonitor: failed to touch session {session_id}",
                        exc_info=True,
                    )
                continue

            logger.info(
                f"Detected dead parent PID {parent_pid} for session {session_id} - expiring",
            )

            await self._expire_session(session_id)
            self._recently_handled[session_id] = now

    def _get_active_sessions_with_pid(
        self,
    ) -> list[tuple[str, int, str | None, str | None]]:
        """Query active/paused sessions that have a parent_pid in terminal_context.

        Returns:
            List of (session_id, parent_pid, tmux_pane, tmux_socket_path) tuples.
            tmux_pane is None when the session has no tmux pane.
        """
        try:
            rows = self._session_manager.db.fetchall(
                """
                SELECT id, terminal_context
                FROM sessions
                WHERE status IN ('active', 'paused')
                AND terminal_context IS NOT NULL
                AND agent_run_id IS NULL
                """,
            )
        except Exception:
            logger.warning(
                "SessionLivenessMonitor: failed to query active sessions",
                exc_info=True,
            )
            return []

        result: list[tuple[str, int, str | None, str | None]] = []
        for row in rows:
            raw_ctx = row["terminal_context"]
            if not raw_ctx:
                continue
            try:
                ctx = json.loads(raw_ctx) if isinstance(raw_ctx, str) else raw_ctx
            except (json.JSONDecodeError, TypeError):
                continue

            parent_pid = ctx.get("parent_pid")
            if isinstance(parent_pid, int) and parent_pid > 0:
                tmux_pane = ctx.get("tmux_pane")
                if tmux_pane is not None and not isinstance(tmux_pane, str):
                    tmux_pane = None
                tmux_socket_path = ctx.get("tmux_socket_path")
                if tmux_socket_path is not None and not isinstance(tmux_socket_path, str):
                    tmux_socket_path = None
                result.append((row["id"], parent_pid, tmux_pane, tmux_socket_path))

        return result

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """Check if a process is still running."""
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we can't signal it — it's alive
            return True
        except OSError:
            return False

    @staticmethod
    def _is_tmux_pane_alive(pane_id: str, socket_path: str | None = None) -> bool:
        """Check if a tmux pane is still alive.

        Args:
            pane_id: Tmux pane identifier (e.g. ``%6``).
            socket_path: Exact tmux socket path, when known.

        Returns:
            True if the pane exists in any tmux session, False otherwise
            (including when tmux is not installed or the server isn't running).
        """
        socket_names = [TmuxConfig().socket_name or "gobby"]
        candidate_commands: list[list[str]] = []
        if socket_path:
            candidate_commands.append(["tmux", "-S", socket_path])
        else:
            candidate_commands.append(["tmux"])
            candidate_commands.extend([["tmux", "-L", socket_name] for socket_name in socket_names])

        try:
            for command in candidate_commands:
                result = subprocess.run(
                    [*command, "list-panes", "-a", "-F", "#{pane_id}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if pane_id in result.stdout.splitlines():
                    return True
            return False
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    async def _expire_session(self, session_id: str) -> None:
        """Dispatch summaries and expire a session."""
        # 1. Dispatch summary generation while JSONL still exists
        if self._dispatch_summaries_fn:
            try:
                self._dispatch_summaries_fn(session_id, False, None)
            except Exception:
                logger.warning(
                    f"SessionLivenessMonitor: summary dispatch failed for {session_id}",
                    exc_info=True,
                )
        elif self._generate_summaries_fn:
            try:
                await self._generate_summaries_fn(session_id)
            except Exception:
                logger.warning(
                    f"SessionLivenessMonitor: summary generation failed for {session_id}",
                    exc_info=True,
                )

        # 2. Mark session as expired
        try:
            self._session_manager.update_status(session_id, "expired")
        except Exception:
            logger.warning(
                f"SessionLivenessMonitor: failed to expire session {session_id}",
                exc_info=True,
            )

        # 3. Unregister from message processor
        if self._message_processor:
            try:
                self._message_processor.unregister_session(session_id)
            except Exception:
                logger.debug(
                    f"SessionLivenessMonitor: failed to unregister session {session_id}",
                    exc_info=True,
                )
