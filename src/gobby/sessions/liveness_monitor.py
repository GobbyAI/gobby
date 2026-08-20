"""CLI session liveness monitor.

Polls active sessions to detect when the owning terminal disappears.
Interactive tmux sessions use their stable window as the authoritative
liveness signal. Sessions on Gobby's configured spawn socket retain pane-based
lifecycle behavior. Parent PID checks cover non-tmux rows.

This is the fast-path counterpart to the 24-hour stale-session expiry in
SessionLifecycleManager, reducing the detection window from hours to
~30 seconds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.agents.tmux.session_manager import TmuxReleaseOutcome
from gobby.config.tmux import TmuxConfig
from gobby.sessions.tmux_context import (
    get_tmux_session_name,
    get_tmux_socket_name,
    get_tmux_window_id,
)
from gobby.storage.hub.postgres_pool import is_pool_unavailable
from gobby.terminal_ownership import (
    TERMINAL_OWNER_STATUSES,
    OwnershipState,
    inspect_foreground_ownership,
    log_pane_ownership_decision,
    resolve_pane_ownership,
    terminal_session_identity,
)
from gobby.terminals.lookup import manager_for_terminal_context
from gobby.utils.logging import ThrottledLogger

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
    source: str | None
    parent_pid: int | None
    tmux_pane: str | None
    tmux_socket_path: str | None
    tmux_socket_name: str | None = None
    tmux_window_id: str | None = None
    tmux_session: str | None = None
    status: str = "active"
    machine_id: str | None = None
    terminal_context: dict[str, Any] | None = None


@dataclass(frozen=True)
class _TmuxSocketIdentity:
    socket_path: str | None
    socket_name: str | None


@dataclass
class _TmuxLivenessInventory:
    live_windows: set[str]
    live_panes: set[str]
    window_by_pane: dict[str, str]
    active_pane_by_window: dict[str, str]
    session_by_window: dict[str, str]


class SessionLivenessMonitor:
    """Background task that detects dead CLI sessions via terminal liveness.

    When the owning process for a non-tmux session exits (e.g. user typed
    ``/exit``, process crashed, terminal closed) or the recorded tmux pane is
    destroyed, this monitor:

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
        tmux_config: TmuxConfig | None = None,
    ) -> None:
        self._session_manager = session_storage
        self._dispatch_summaries_fn = dispatch_summaries_fn
        self._generate_summaries_fn = generate_summaries_fn
        self._message_processor_resolver = message_processor_resolver or (lambda: None)
        self._poll_interval = poll_interval
        self._tmux_config = tmux_config
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

        inventories = await asyncio.to_thread(
            self._get_tmux_inventories_by_socket,
            active_sessions,
        )

        pane_groups: dict[
            tuple[str, str, str],
            tuple[list[_TerminalLivenessRecord], _TmuxLivenessInventory],
        ] = {}

        for record in active_sessions:
            if record.session_id in self._recently_handled:
                continue

            has_tmux_target = bool(record.tmux_pane or getattr(record, "tmux_window_id", None))
            if has_tmux_target:
                socket = self._socket_identity(record)
                inventory = inventories.get(socket)
                if inventory is None:
                    # A failed tmux probe is inconclusive, so preserve lifecycle
                    # and title state until the next sweep.
                    continue
                repaired = await self._repair_tmux_target(record, inventory)
                if repaired is None:
                    if await self._expire_record(record, now):
                        await self._release_tmux_title(record)
                    continue
                record = repaired
                identity = terminal_session_identity(record)
                if identity is None:
                    # A live tmux target is sufficient lifecycle evidence even
                    # when persisted socket metadata cannot form a group key.
                    continue

                records, existing_inventory = pane_groups.setdefault(
                    identity,
                    ([], inventory),
                )
                records.append(record)
                if existing_inventory is not inventory:
                    logger.warning("Conflicting tmux socket classification for %s", identity)
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
            await self._expire_record(record, now)

        for records, inventory in pane_groups.values():
            await self._handle_live_pane_group(
                records,
                now,
                inventory=inventory,
            )

    async def _handle_live_pane_group(
        self,
        records: list[_TerminalLivenessRecord],
        now: float,
        *,
        inventory: _TmuxLivenessInventory,
    ) -> None:
        """Preserve live-pane rows while retaining canonical owner selection."""
        decision = await asyncio.to_thread(
            resolve_pane_ownership,
            list(records),
            requested_session_id=records[0].session_id,
        )
        log_pane_ownership_decision(logger, decision)
        owner = decision.owner
        if not isinstance(owner, _TerminalLivenessRecord):
            return

        repaired = await self._repair_tmux_target(owner, inventory)
        if repaired is None and await self._expire_record(owner, now):
            await self._release_tmux_title(owner)

    async def _expire_record(
        self,
        record: _TerminalLivenessRecord,
        now: float,
    ) -> bool:
        if getattr(record, "status", "active") not in TERMINAL_OWNER_STATUSES:
            return False
        if not await self._expire_session(record.session_id):
            return False
        self._recently_handled[record.session_id] = now
        return True

    async def _release_tmux_title(self, record: _TerminalLivenessRecord) -> None:
        context = record.terminal_context
        target = record.tmux_pane or record.tmux_window_id
        if context is None or target is None:
            return
        manager = manager_for_terminal_context(context)
        for _attempt in range(2):
            try:
                outcome = await manager.release_window_title_ownership(target)
            except Exception:
                logger.debug(
                    "SessionLivenessMonitor: failed to release tmux title for %s",
                    record.session_id,
                    exc_info=True,
                )
                return
            if outcome in {
                TmuxReleaseOutcome.RELEASED,
                TmuxReleaseOutcome.ALREADY_RELEASED,
            }:
                return

    async def _repair_tmux_target(
        self,
        record: _TerminalLivenessRecord,
        inventory: _TmuxLivenessInventory,
    ) -> _TerminalLivenessRecord | None:
        """Backfill stable window identity and repair a replaced pane."""
        recorded_window = getattr(record, "tmux_window_id", None)
        recorded_session = getattr(record, "tmux_session", None)
        window_id = recorded_window if recorded_window in inventory.live_windows else None
        pane_id = record.tmux_pane if record.tmux_pane in inventory.live_panes else None

        if window_id is not None:
            if pane_id is None or inventory.window_by_pane.get(pane_id) != window_id:
                pane_id = inventory.active_pane_by_window.get(window_id)
        elif pane_id is not None:
            window_id = inventory.window_by_pane.get(pane_id)

        if window_id is None or pane_id is None:
            return None

        tmux_session = inventory.session_by_window.get(window_id) or recorded_session
        context = dict(record.terminal_context or {})
        patch: dict[str, str] = {}
        if recorded_window != window_id:
            patch["tmux_window_id"] = window_id
        if record.tmux_pane != pane_id:
            patch["tmux_pane"] = pane_id
        if tmux_session and recorded_session != tmux_session:
            patch["tmux_session"] = tmux_session

        if patch:
            try:
                await asyncio.to_thread(
                    self._session_manager.update,
                    record.session_id,
                    terminal_context=patch,
                )
            except Exception:
                logger.warning(
                    "SessionLivenessMonitor: failed to repair tmux target for %s",
                    record.session_id,
                    exc_info=True,
                )
            context.update(patch)

        return _TerminalLivenessRecord(
            session_id=record.session_id,
            source=record.source,
            parent_pid=record.parent_pid,
            tmux_pane=pane_id,
            tmux_socket_path=record.tmux_socket_path,
            tmux_socket_name=getattr(record, "tmux_socket_name", None),
            tmux_window_id=window_id,
            tmux_session=tmux_session,
            status=record.status,
            machine_id=record.machine_id,
            terminal_context=context,
        )

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
                SELECT s.id, s.source, s.status, s.machine_id, s.terminal_context
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
            tmux_socket_path = ctx.get("tmux_socket_path")
            if tmux_socket_path is not None and not isinstance(tmux_socket_path, str):
                tmux_socket_path = None
            tmux_socket_name = get_tmux_socket_name(ctx)
            tmux_window_id = get_tmux_window_id(ctx)
            tmux_session = get_tmux_session_name(ctx)

            if parent_pid is None and not tmux_pane and not tmux_window_id:
                continue

            result.append(
                _TerminalLivenessRecord(
                    session_id=row["id"],
                    source=self._row_value(row, "source"),
                    parent_pid=parent_pid,
                    tmux_pane=tmux_pane,
                    tmux_socket_path=tmux_socket_path,
                    tmux_socket_name=tmux_socket_name,
                    tmux_window_id=tmux_window_id,
                    tmux_session=tmux_session,
                    status=self._row_value(row, "status") or "active",
                    machine_id=self._row_value(row, "machine_id"),
                    terminal_context=ctx,
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

    def _configured_tmux_config(self) -> TmuxConfig | None:
        if self._tmux_config is not None:
            return self._tmux_config
        try:
            from gobby.agents.tmux import get_configured_tmux_config

            return get_configured_tmux_config()
        except RuntimeError:
            return None

    @staticmethod
    def _socket_identity(record: _TerminalLivenessRecord) -> _TmuxSocketIdentity:
        return _TmuxSocketIdentity(
            record.tmux_socket_path,
            getattr(record, "tmux_socket_name", None),
        )

    def _tmux_commands_for_socket(self, socket: _TmuxSocketIdentity) -> list[list[str]]:
        config = self._configured_tmux_config()
        command = config.command if config is not None else "tmux"
        if socket.socket_path:
            return [[command, "-S", socket.socket_path]]
        if socket.socket_name:
            return [[command, "-L", socket.socket_name]]

        commands = [[command]]
        if config is not None:
            configured = [command]
            if config.socket_path:
                configured.extend(["-S", config.socket_path])
            elif config.socket_name:
                configured.extend(["-L", config.socket_name])
            if configured not in commands:
                commands.append(configured)
        return commands

    @staticmethod
    def _empty_tmux_inventory() -> _TmuxLivenessInventory:
        return _TmuxLivenessInventory(set(), set(), {}, {}, {})

    @classmethod
    def _parse_tmux_inventory(cls, output: str) -> _TmuxLivenessInventory:
        inventory = cls._empty_tmux_inventory()
        for line in output.splitlines():
            fields = line.rstrip().split("\t")
            if len(fields) != 5:
                continue
            session_name, window_id, pane_id, pane_active, pane_dead = fields
            if not session_name or not window_id or not pane_id or pane_dead == "1":
                continue
            inventory.live_windows.add(window_id)
            inventory.live_panes.add(pane_id)
            inventory.window_by_pane[pane_id] = window_id
            inventory.session_by_window[window_id] = session_name
            if pane_active == "1" or window_id not in inventory.active_pane_by_window:
                inventory.active_pane_by_window[window_id] = pane_id
        return inventory

    def _list_tmux_inventory(
        self,
        socket: _TmuxSocketIdentity,
    ) -> _TmuxLivenessInventory | None:
        """Return live windows and panes for one recorded tmux socket."""
        inventory = self._empty_tmux_inventory()
        tmux_format = "#{session_name}\t#{window_id}\t#{pane_id}\t#{pane_active}\t#{pane_dead}"
        for command in self._tmux_commands_for_socket(socket):
            try:
                result = subprocess.run(
                    [*command, "list-panes", "-a", "-F", tmux_format],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                return None
            if result.returncode != 0:
                return None
            current = self._parse_tmux_inventory(result.stdout)
            inventory.live_windows.update(current.live_windows)
            inventory.live_panes.update(current.live_panes)
            inventory.window_by_pane.update(current.window_by_pane)
            inventory.active_pane_by_window.update(current.active_pane_by_window)
            inventory.session_by_window.update(current.session_by_window)
        return inventory

    def _get_tmux_inventories_by_socket(
        self,
        records: list[_TerminalLivenessRecord],
    ) -> dict[_TmuxSocketIdentity, _TmuxLivenessInventory | None]:
        """Probe each recorded tmux socket once per poll."""
        sockets = {
            self._socket_identity(record)
            for record in records
            if record.tmux_pane or getattr(record, "tmux_window_id", None)
        }
        return {socket: self._list_tmux_inventory(socket) for socket in sockets}

    async def _expire_session(self, session_id: str) -> bool:
        """Conditionally expire a session, then dispatch cleanup work."""
        try:
            expired_session = await asyncio.to_thread(
                self._session_manager.expire_if_active,
                session_id,
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
