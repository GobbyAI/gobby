"""CLI session liveness monitor.

Polls active sessions to detect when the owning terminal disappears.
A session in a tmux pane lives as long as its pane, or the window it was
replaced in, on the recorded tmux server. Parent PID checks cover the rest.

This is the fast-path counterpart to the 24-hour stale-session expiry in
SessionLifecycleManager, reducing the detection window from hours to
~30 seconds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from gobby.agents.tmux.session_manager import TmuxPaneInfo, TmuxSessionManager
from gobby.sessions.tmux_context import get_tmux_window_id
from gobby.storage.hook_receipts import retire_session_hook_effects
from gobby.storage.hub.postgres_pool import is_pool_unavailable
from gobby.terminal_ownership import (
    TERMINAL_OWNER_STATUSES,
    OwnershipState,
    inspect_foreground_ownership,
)
from gobby.utils.logging import ThrottledLogger
from gobby.utils.machine_id import get_machine_id

if TYPE_CHECKING:
    from gobby.sessions.processor import SessionMessageProcessor
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

# How long a session_id stays in the recently-handled set (seconds)
_RECENTLY_HANDLED_TTL = 120.0
_pool_outage_log = ThrottledLogger()

# Default polling interval (seconds)
_DEFAULT_POLL_INTERVAL = 30.0


@dataclass(frozen=True)
class _TerminalLivenessRecord:
    session_id: str
    parent_pid: int | None
    tmux_pane: str | None
    tmux_window_id: str | None = None
    status: str = "active"
    machine_id: str | None = None
    terminal_context: dict[str, Any] | None = None
    updated_at: datetime | None = None


class SessionLivenessMonitor:
    """Background task that detects dead CLI sessions via terminal liveness.

    When the owning process for a session exits (e.g. user typed ``/exit``,
    process crashed, terminal closed) or its tmux pane is gone, this monitor:

    1. Dispatches summary generation while the transcript file is still fresh.
    2. Marks the session as ``expired``.
    3. Unregisters the session from the message processor.

    Args:
        session_storage: Session manager for DB queries and status updates.
        dispatch_summaries_fn: Callback to generate session summaries.
            Signature: ``(session_id: str, background: bool, done_event) -> None``
        message_processor_resolver: Resolves the current message processor for cleanup.
        poll_interval: Seconds between polls (default 30).
    """

    def __init__(
        self,
        session_storage: SessionManager,
        dispatch_summaries_fn: Callable[..., None] | None = None,
        generate_summaries_fn: Callable[..., Coroutine[Any, Any, None]] | None = None,
        message_processor_resolver: Callable[[], SessionMessageProcessor | None] | None = None,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        terminal_manager: Any | None = None,
        startup_ready: Callable[[], bool] | None = None,
        live_host_epoch: Callable[[], str | None] | None = None,
    ) -> None:
        self._session_manager = session_storage
        self._dispatch_summaries_fn = dispatch_summaries_fn
        self._generate_summaries_fn = generate_summaries_fn
        self._message_processor_resolver = message_processor_resolver or (lambda: None)
        self._poll_interval = poll_interval
        self.terminal_manager = terminal_manager
        self._startup_ready = startup_ready
        self._live_host_epoch = live_host_epoch or (lambda: None)
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
        logger.info("SessionLivenessMonitor started (interval=%.0fs)", self._poll_interval)

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
        # A parked session may have no terminal while startup recovery resumes it.
        if self._startup_ready is not None and not self._startup_ready():
            return
        now = time.monotonic()
        expired = [
            sid for sid, ts in self._recently_handled.items() if now - ts > _RECENTLY_HANDLED_TTL
        ]
        for sid in expired:
            del self._recently_handled[sid]

        # 2. Query active sessions with terminal_context
        active_sessions = await asyncio.to_thread(self._get_active_terminal_sessions)
        if not active_sessions:
            return

        # One list-panes per tmux socket per sweep.
        tmux_panes: dict[str, list[TmuxPaneInfo] | None] = {}
        local_machine_id = get_machine_id()
        for record in active_sessions:
            if record.session_id in self._recently_handled:
                continue
            # Pids and tmux sockets are only meaningful on the machine that recorded them.
            if (
                local_machine_id is None
                or record.machine_id != local_machine_id
                or record.updated_at is None
            ):
                continue

            native_id = (record.terminal_context or {}).get("gobby_terminal_id")
            if record.status == "paused" and isinstance(native_id, str) and native_id:
                if self.terminal_manager is None:
                    continue
                try:
                    live = await asyncio.to_thread(
                        self.terminal_manager.get_live_for_session, record.session_id
                    )
                except Exception:
                    logger.warning(
                        "SessionLivenessMonitor: failed to inspect terminal for session %s",
                        record.session_id,
                        exc_info=True,
                    )
                    continue
                if live is None and await self._expire_session(
                    record.session_id,
                    native_expiry=(native_id, local_machine_id, record.updated_at),
                ):
                    self._recently_handled[record.session_id] = now
                continue

            has_tmux_target = bool(record.tmux_pane or getattr(record, "tmux_window_id", None))
            if has_tmux_target:
                # Only a definite "gone" expires; an unanswered probe keeps the session.
                if await self._tmux_target_live(record, tmux_panes) is False:
                    await self._expire_record(record, local_machine_id, now)
                continue

            inspection = await asyncio.to_thread(
                inspect_foreground_ownership,
                record,
            )
            if inspection.state is OwnershipState.INDETERMINATE:
                continue
            if inspection.state is OwnershipState.OWNED:
                continue

            logger.info(
                "Detected ownerless terminal process %s for session %s - expiring",
                record.parent_pid,
                record.session_id,
            )
            await self._expire_record(record, local_machine_id, now)

    @staticmethod
    async def _tmux_target_live(
        record: _TerminalLivenessRecord,
        probes: dict[str, list[TmuxPaneInfo] | None],
    ) -> bool | None:
        """Whether the session's tmux pane, or its window, survives on its server.

        ``None`` when the session recorded no socket or tmux did not answer.
        """
        context = record.terminal_context or {}
        socket_path = context.get("tmux_socket_path")
        if not isinstance(socket_path, str) or not socket_path:
            return None
        if socket_path not in probes:
            try:
                probes[socket_path] = await TmuxSessionManager(socket_path).list_panes()
            except (TimeoutError, OSError):
                probes[socket_path] = None
        panes = probes[socket_path]
        if panes is None:
            return None
        server_pid = context.get("tmux_server_pid")
        server_start_time = context.get("tmux_server_start_time")
        # A restarted server reuses pane ids, so a recorded generation must match.
        check_server = isinstance(server_pid, int) and isinstance(server_start_time, int)
        return any(
            not pane.pane_dead
            and (pane.pane_id == record.tmux_pane or pane.window_id == record.tmux_window_id)
            and (
                not check_server
                or (pane.server_pid, pane.server_start_time) == (server_pid, server_start_time)
            )
            for pane in panes
        )

    async def _expire_record(
        self,
        record: _TerminalLivenessRecord,
        machine_id: str,
        now: float,
    ) -> bool:
        if getattr(record, "status", "active") not in TERMINAL_OWNER_STATUSES:
            return False
        if record.updated_at is None:
            return False
        if not await self._expire_session(
            record.session_id, active_expiry=(machine_id, record.updated_at)
        ):
            return False
        self._recently_handled[record.session_id] = now
        return True

    def _get_active_terminal_sessions(
        self,
    ) -> list[_TerminalLivenessRecord]:
        """Query eligible sessions with terminal liveness metadata.

        Returns:
            Records containing a session ID plus optional parent PID and tmux pane.
        """
        try:
            rows = self._session_manager.db.fetchall(
                """
                SELECT s.id, s.status, s.machine_id, s.updated_at,
                       s.terminal_context
                FROM sessions s
                LEFT JOIN agent_runs ar ON ar.id = s.agent_run_id
                WHERE s.status = ANY(%s)
                AND s.terminal_context IS NOT NULL
                AND (
                    s.agent_run_id IS NULL
                    OR ar.id IS NULL
                    OR ar.status NOT IN ('running', 'pending')
                )
                """,
                (list(TERMINAL_OWNER_STATUSES),),
            )
        except Exception as exc:
            if is_pool_unavailable(exc):
                _pool_outage_log(
                    logger,
                    logging.WARNING,
                    "SessionLivenessMonitor: hub temporarily unavailable; skipping pass",
                )
            else:
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
            tmux_window_id = get_tmux_window_id(ctx)

            native_id = ctx.get("gobby_terminal_id")
            native_candidate = (
                self._row_value(row, "status") == "paused"
                and isinstance(native_id, str)
                and bool(native_id)
            )
            if parent_pid is None and not tmux_pane and not tmux_window_id and not native_candidate:
                continue

            result.append(
                _TerminalLivenessRecord(
                    session_id=row["id"],
                    parent_pid=parent_pid,
                    tmux_pane=tmux_pane,
                    tmux_window_id=tmux_window_id,
                    status=self._row_value(row, "status") or "active",
                    machine_id=self._row_value(row, "machine_id"),
                    terminal_context=ctx,
                    updated_at=self._row_value(row, "updated_at"),
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
    def _row_value(row: Any, key: str) -> Any:
        if isinstance(row, dict):
            return row.get(key)
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return None

    async def _expire_session(
        self,
        session_id: str,
        *,
        active_expiry: tuple[str, datetime] | None = None,
        native_expiry: tuple[str, str, datetime] | None = None,
    ) -> bool:
        """Conditionally expire a session, then dispatch cleanup work.

        Exactly one of ``active_expiry`` (machine id, observed ``updated_at``) or
        ``native_expiry`` names the snapshot the expiry is conditional on.
        """
        try:
            if native_expiry is None:
                if active_expiry is None:
                    raise ValueError("expiry needs an observed snapshot")
                machine_id, observed_updated_at = active_expiry
                expired_session = await asyncio.to_thread(
                    self._session_manager.expire_if_active,
                    session_id,
                    machine_id=machine_id,
                    observed_updated_at=observed_updated_at,
                )
            else:
                terminal_id, machine_id, observed_updated_at = native_expiry
                expired_session = await asyncio.to_thread(
                    self._session_manager.expire_if_paused_terminal_exited,
                    session_id,
                    terminal_id=terminal_id,
                    machine_id=machine_id,
                    observed_updated_at=observed_updated_at,
                    live_host_epoch=self._live_host_epoch(),
                )
        except Exception:
            logger.warning(
                "SessionLivenessMonitor: failed to expire session %s",
                session_id,
                exc_info=True,
            )
            return False
        if expired_session is None:
            return False

        try:
            retire_session_hook_effects(self._session_manager.db, session_id=session_id)
        except Exception:
            logger.warning(
                "SessionLivenessMonitor: failed to retire hook effects for session %s",
                session_id,
                exc_info=True,
            )

        manager = self.terminal_manager
        if manager is not None:
            try:
                row = manager.get_live_for_session(session_id)
                if row is not None and row.ownership == "gobby" and row.agent_run_id is None:
                    # The pane outlives the CLI; the next session started in it rebinds,
                    # and so does this session's revival if the CLI was alive after all.
                    manager.release_session(row.id, session_id)
                elif row is not None:
                    manager.mark_exited(row.id)
            except Exception:
                logger.warning(
                    "SessionLivenessMonitor: failed to CAS terminal for session %s",
                    session_id,
                    exc_info=True,
                )

        if self._dispatch_summaries_fn:
            try:
                self._dispatch_summaries_fn(session_id, False, None)
            except Exception:
                logger.warning(
                    "SessionLivenessMonitor: summary dispatch failed for %s",
                    session_id,
                    exc_info=True,
                )
        elif self._generate_summaries_fn:
            try:
                await self._generate_summaries_fn(session_id)
            except Exception:
                logger.warning(
                    "SessionLivenessMonitor: summary generation failed for %s",
                    session_id,
                    exc_info=True,
                )

        try:
            message_processor = self._message_processor_resolver()
            if message_processor is not None:
                message_processor.unregister_session(session_id)
        except Exception:
            logger.debug(
                "SessionLivenessMonitor: failed to unregister session %s",
                session_id,
                exc_info=True,
            )
        return True
