"""CLI session liveness monitor.

Polls active sessions to detect when the owning terminal disappears. For
tmux-backed sessions, the recorded pane is the primary liveness signal; for
plain terminals, the parent CLI process is used. When liveness is gone the
session is expired and summary generation is dispatched while the JSONL
transcript file still exists on disk.

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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _TerminalLivenessRecord:
    session_id: str
    parent_pid: int | None
    tmux_pane: str | None
    tmux_socket_path: str | None


class SessionLivenessMonitor:
    """Background task that detects dead CLI sessions via terminal liveness.

    When the parent process that owns a session exits (e.g. user typed
    ``/exit``, process crashed, terminal closed) or the recorded tmux pane is
    destroyed, this monitor:

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
        """Check active sessions for dead terminal owners."""
        # 1. Prune expired entries from recently-handled set
        now = time.monotonic()
        expired = [
            sid for sid, ts in self._recently_handled.items() if now - ts > _RECENTLY_HANDLED_TTL
        ]
        for sid in expired:
            del self._recently_handled[sid]

        # 2. Query active sessions with terminal_context
        active_sessions = self._get_active_terminal_sessions()
        if not active_sessions:
            return

        live_panes_by_socket = self._get_live_tmux_panes_by_socket(active_sessions)

        # 3. Prefer tmux pane liveness when available; fallback to parent PID.
        for record in active_sessions:
            if record.session_id in self._recently_handled:
                continue

            if record.tmux_pane:
                live_panes = live_panes_by_socket.get(record.tmux_socket_path)
                if live_panes is None:
                    # tmux command failed unexpectedly. Avoid mass-expiring live sessions;
                    # PID fallback below can still catch plainly dead terminals.
                    if record.parent_pid is not None and self._is_pid_alive(record.parent_pid):
                        continue
                elif record.tmux_pane not in live_panes:
                    logger.info(
                        f"Detected missing tmux pane {record.tmux_pane} "
                        f"for session {record.session_id} - expiring",
                    )
                    await self._expire_session(record.session_id)
                    self._recently_handled[record.session_id] = now
                    continue
                else:
                    if record.parent_pid is None or not self._is_pid_alive(record.parent_pid):
                        logger.debug(
                            f"Session {record.session_id} parent PID {record.parent_pid} "
                            f"dead/missing but tmux pane {record.tmux_pane} alive - refreshing",
                        )
                        try:
                            self._session_manager.touch(record.session_id)
                        except Exception:
                            logger.warning(
                                "SessionLivenessMonitor: failed to touch session "
                                f"{record.session_id}",
                                exc_info=True,
                            )
                    continue

            if record.parent_pid is None:
                continue

            if self._is_pid_alive(record.parent_pid):
                continue

            logger.info(
                f"Detected dead parent PID {record.parent_pid} "
                f"for session {record.session_id} - expiring",
            )

            await self._expire_session(record.session_id)
            self._recently_handled[record.session_id] = now

    def _get_active_terminal_sessions(
        self,
    ) -> list[_TerminalLivenessRecord]:
        """Query active/paused sessions that have terminal liveness metadata.

        Returns:
            Records containing a session ID plus optional parent PID and tmux pane.
        """
        try:
            rows = self._session_manager.db.fetchall(
                """
                SELECT s.id, s.terminal_context
                FROM sessions s
                LEFT JOIN agent_runs ar ON ar.id = s.agent_run_id
                WHERE s.status IN ('active', 'paused')
                AND s.terminal_context IS NOT NULL
                AND (
                    s.agent_run_id IS NULL
                    OR ar.id IS NULL
                    OR ar.status NOT IN ('running', 'pending')
                )
                """,
            )
        except Exception:
            logger.warning(
                "SessionLivenessMonitor: failed to query active sessions",
                exc_info=True,
            )
            return []

        result: list[_TerminalLivenessRecord] = []
        for row in rows:
            raw_ctx = row["terminal_context"]
            if not raw_ctx:
                continue
            try:
                ctx = json.loads(raw_ctx) if isinstance(raw_ctx, str) else raw_ctx
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(ctx, dict):
                continue

            parent_pid = self._normalize_parent_pid(ctx.get("parent_pid"))
            tmux_pane = ctx.get("tmux_pane")
            if tmux_pane is not None and not isinstance(tmux_pane, str):
                tmux_pane = None
            tmux_socket_path = ctx.get("tmux_socket_path")
            if tmux_socket_path is not None and not isinstance(tmux_socket_path, str):
                tmux_socket_path = None

            if parent_pid is None and not tmux_pane:
                continue

            result.append(
                _TerminalLivenessRecord(
                    session_id=row["id"],
                    parent_pid=parent_pid,
                    tmux_pane=tmux_pane,
                    tmux_socket_path=tmux_socket_path,
                )
            )

        return result

    @staticmethod
    def _normalize_parent_pid(value: Any) -> int | None:
        """Return a usable parent PID from terminal_context."""
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit():
            pid = int(value)
            return pid if pid > 0 else None
        return None

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
        live_panes = SessionLivenessMonitor._list_tmux_panes(socket_path)
        return live_panes is not None and pane_id in live_panes

    @staticmethod
    def _list_tmux_panes(socket_path: str | None = None) -> set[str] | None:
        """Return live pane IDs for a tmux server, or None when the command failed."""
        socket_names = [TmuxConfig().socket_name or "gobby"]
        candidate_commands: list[list[str]]
        if socket_path:
            candidate_commands = [["tmux", "-S", socket_path]]
        else:
            candidate_commands = [["tmux"]]
            candidate_commands.extend([["tmux", "-L", socket_name] for socket_name in socket_names])

        live_panes: set[str] = set()
        for command in candidate_commands:
            try:
                result = subprocess.run(
                    [*command, "list-panes", "-a", "-F", "#{pane_id}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                return None
            live_panes.update(line.strip() for line in result.stdout.splitlines() if line.strip())
        return live_panes

    def _get_live_tmux_panes_by_socket(
        self,
        records: list[_TerminalLivenessRecord],
    ) -> dict[str | None, set[str] | None]:
        """Batch tmux pane liveness by recorded socket path."""
        socket_paths = {record.tmux_socket_path for record in records if record.tmux_pane}
        return {
            socket_path: self._list_tmux_panes(socket_path)
            for socket_path in socket_paths
        }

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
