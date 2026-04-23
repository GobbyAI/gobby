"""Tmux pane death monitor.

Polls ``tmux -L gobby list-sessions`` to detect when agent tmux sessions
disappear (process exit, crash, user kill-pane) and synthesizes
``session_end`` events so the full teardown flow runs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig
from gobby.hooks.events import HookEvent, HookEventType, SessionSource

if TYPE_CHECKING:
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

# How long (seconds) a session_id stays in the recently-ended set
_RECENTLY_ENDED_TTL = 60.0


class TmuxPaneMonitor:
    """Background task that detects dead tmux panes and fires session_end.

    Args:
        session_end_callback: Called with a synthesized :class:`HookEvent`
            when a tmux session vanishes.  Typically
            ``EventHandlers.handle_session_end``.
        config: Tmux configuration (socket name, binary path, etc.).
        poll_interval: Seconds between polls (default 5).
    """

    def __init__(
        self,
        session_end_callback: Callable[[HookEvent], Any],
        config: TmuxConfig | None = None,
        poll_interval: float = 5.0,
        session_manager: SessionManager | None = None,
    ) -> None:
        self._callback = session_end_callback
        self._config = config or TmuxConfig()
        self._poll_interval = poll_interval
        self._session_manager = session_manager
        self._task: asyncio.Task[None] | None = None
        # session_id -> timestamp when it was marked ended
        self._recently_ended: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background polling task."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._poll_loop(), name="tmux-pane-monitor")
        logger.info(f"TmuxPaneMonitor started (interval={self._poll_interval:.1f}s)")

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
        logger.info("TmuxPaneMonitor stopped")

    def mark_recently_ended(self, session_id: str) -> None:
        """Record that *session_id* just had a normal session_end.

        This prevents the monitor from firing a duplicate event when it
        next polls and notices the tmux session is gone.
        """
        self._recently_ended[session_id] = time.monotonic()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Infinite loop: sleep, check panes, repeat."""
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                await self._check_panes()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("TmuxPaneMonitor poll error (continuing)")

    async def _check_panes(self) -> None:
        """Core detection: cross-reference live tmux sessions with DB agent runs."""
        from gobby.storage.agents import LocalAgentRunManager

        # 1. Prune expired entries from recently-ended set
        now = time.monotonic()
        expired = [
            sid for sid, ts in self._recently_ended.items() if now - ts > _RECENTLY_ENDED_TTL
        ]
        for sid in expired:
            del self._recently_ended[sid]

        # 2. Get live tmux sessions (includes pane_dead status)
        mgr = TmuxSessionManager(self._config)
        try:
            live_sessions = await mgr.list_sessions()
        except Exception:
            logger.warning("TmuxPaneMonitor: failed to list tmux sessions", exc_info=True)
            return
        live_lookup = {s.name: s for s in live_sessions}

        # 3. Get all active agent runs with a tmux_session_name from DB
        if not self._session_manager:
            return
        try:
            arm = LocalAgentRunManager(self._session_manager.db)
            all_runs = arm.list_active()
        except Exception:
            logger.warning("TmuxPaneMonitor: failed to list active agent runs", exc_info=True)
            return
        tmux_agents = [r for r in all_runs if r.tmux_session_name]

        if not tmux_agents:
            return

        # 4. Fire session_end for agents whose tmux session is gone,
        #    whose pane process has exited (remain-on-exit keeps session alive),
        #    or whose registered PID is no longer running.
        for agent in tmux_agents:
            session_name = agent.tmux_session_name
            if session_name is None:
                continue
            live_info = live_lookup.get(session_name)

            # Check if the agent's PID is still alive (catches remain-on-exit cases)
            pid_dead = False
            if live_info and not live_info.pane_dead and agent.pid:
                try:
                    os.kill(agent.pid, 0)
                except ProcessLookupError:
                    pid_dead = True
                    logger.info(
                        f"Agent PID {agent.pid} is dead but tmux session {session_name} still alive (remain-on-exit)",
                    )
                except PermissionError:
                    pass  # PID exists but we can't signal it — it's alive

            if live_info and not live_info.pane_dead and not pid_dead:
                continue
            child_sid = agent.child_session_id or agent.id
            if child_sid in self._recently_ended:
                continue

            logger.info(
                f"Detected dead tmux pane for agent session={child_sid} (tmux={agent.tmux_session_name})",
            )

            # Look up the session to get external_id and source
            session = self._lookup_session(child_sid)
            if session is None:
                logger.warning(
                    f"Cannot synthesize session_end: session {child_sid} not found in DB",
                )
                self._recently_ended[child_sid] = now
                continue

            event = HookEvent(
                event_type=HookEventType.SESSION_END,
                session_id=session.external_id,
                source=SessionSource(session.source) if session.source else SessionSource.CLAUDE,
                timestamp=datetime.now(UTC),
                data={"cwd": None},
                metadata={
                    "_platform_session_id": session.id,
                    "_tmux_pane_death": True,
                },
            )

            try:
                self._callback(event)
            except Exception:
                logger.exception(f"TmuxPaneMonitor: callback error for session {child_sid}")

            self._recently_ended[child_sid] = now

    def _lookup_session(self, session_id: str) -> Session | None:
        """Look up a session from the database."""
        if not self._session_manager:
            logger.debug(f"No _session_manager configured, cannot look up {session_id}")
            return None
        try:
            return self._session_manager.get(session_id)
        except Exception:
            logger.debug(f"Failed to look up session {session_id}", exc_info=True)
            return None
