"""Tmux pane death monitor.

Polls ``tmux -L gobby list-sessions`` to detect when agent tmux sessions
disappear (process exit, crash, user kill-pane) and synthesizes
``session_end`` events so the full teardown flow runs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.agents.detection.provider import DetectionRegistry
from gobby.agents.kill import pid_matches_agent_identity
from gobby.agents.prompt_detector import PromptDetector, PromptKind
from gobby.agents.stall_classifier import StallClassifier, StallStatus
from gobby.agents.tmux.session_activation import TMUX_COMMAND_TIMEOUT_SECONDS
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig
from gobby.hooks.events import HookEvent, HookEventType, SessionSource, parse_session_source
from gobby.storage.attention import session_attention_entry_id
from gobby.storage.hub.postgres_pool import is_pool_unavailable
from gobby.utils.logging import ThrottledLogger
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.storage.agents import AgentRun, LocalAgentRunManager
    from gobby.storage.attention import AttentionKind, AttentionStateManager
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

# How long (seconds) a session_id stays in the recently-ended set
_RECENTLY_ENDED_TTL = 60.0
_AGENT_RUN_PAGE_SIZE = 100
_INTERACTIVE_SESSION_PAGE_SIZE = 100
_pool_outage_log = ThrottledLogger()


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
        detection_registry: DetectionRegistry,
        config: TmuxConfig | None = None,
        poll_interval: float = 5.0,
        session_manager: SessionManager | None = None,
        attention_manager: AttentionStateManager | None = None,
        prompt_detector: PromptDetector | None = None,
        stall_classifier: StallClassifier | None = None,
        tmux_manager_factory: Callable[[Mapping[str, Any]], TmuxSessionManager] | None = None,
    ) -> None:
        self._callback = session_end_callback
        if config is None:
            from gobby.agents.tmux import get_configured_tmux_config

            try:
                config = get_configured_tmux_config()
            except RuntimeError as exc:
                logger.warning("Configured tmux config unavailable, using defaults: %s", exc)
                config = TmuxConfig()
        self._config = config
        self._poll_interval = poll_interval
        self._session_manager = session_manager
        self._attention_manager = attention_manager
        self._detection_registry = detection_registry
        self._prompt_detector = prompt_detector or PromptDetector(detection_registry)
        self._stall_classifier = stall_classifier or StallClassifier(detection_registry)
        from gobby.terminals.tmux_runtime import TmuxTerminalRuntime

        self._tmux_manager_factory = tmux_manager_factory
        self._runtime = TmuxTerminalRuntime(TmuxSessionManager(config=self._config))
        self._task: asyncio.Task[None] | None = None
        # session_id -> timestamp when it was marked ended
        self._recently_ended: dict[str, float] = {}

    @property
    def detection_registry(self) -> DetectionRegistry:
        return self._detection_registry

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background polling task."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._poll_loop(), name="tmux-pane-monitor")
        logger.info("TmuxPaneMonitor started (interval=%.1fs)", self._poll_interval)

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
        except TimeoutError as exc:
            logger.debug(
                "TmuxPaneMonitor: timed out listing tmux sessions",
                extra={
                    "tmux_command": self._config.command,
                    "tmux_subcommand": "list-sessions",
                    "socket_name": self._config.socket_name,
                    "socket_path": self._config.socket_path,
                    "config_file": self._config.config_file,
                    "timeout_seconds": TMUX_COMMAND_TIMEOUT_SECONDS,
                    "poll_interval_seconds": self._poll_interval,
                    "error": str(exc),
                },
            )
            return
        except Exception:
            logger.warning("TmuxPaneMonitor: failed to list tmux sessions", exc_info=True)
            return
        live_lookup = {s.name: s for s in live_sessions}

        # 3. Get all active agent runs with a live tmux terminal row from DB
        if not self._session_manager:
            return
        try:
            arm = LocalAgentRunManager(self._session_manager.db)
            all_runs = await self._list_active_runs(arm)
        except Exception as exc:
            if is_pool_unavailable(exc):
                _pool_outage_log(
                    logger,
                    logging.WARNING,
                    "TmuxPaneMonitor: hub temporarily unavailable; skipping pass",
                )
            else:
                logger.warning("TmuxPaneMonitor: failed to list active agent runs", exc_info=True)
            return
        await self._check_attention_panes(active_runs=all_runs)
        from gobby.storage.terminals import TerminalManager

        terminal_manager = TerminalManager(self._session_manager.db)
        tmux_agents: list[tuple[Any, str]] = []
        for run in all_runs:
            if not run.terminal_id:
                continue
            row = terminal_manager.get(run.terminal_id)
            if row is None or row.backend != "tmux" or not row.session_name:
                continue
            tmux_agents.append((run, row.session_name))

        if not tmux_agents:
            return

        # 4. Fire session_end for agents whose tmux session is gone,
        #    whose pane process has exited (remain-on-exit keeps session alive),
        #    or whose registered PID is no longer running.
        for agent, session_name in tmux_agents:
            live_info = live_lookup.get(session_name)

            # Check if the agent's PID is still alive (catches remain-on-exit cases)
            pid_dead = False
            if live_info and not live_info.pane_dead and agent.pid:
                session_id = agent.child_session_id or agent.parent_session_id
                if not await pid_matches_agent_identity(
                    agent.pid,
                    provider=agent.provider,
                    session_id=session_id,
                    unverifiable_result=True,
                ):
                    pid_dead = True
                    logger.info(
                        "Agent PID %s no longer matches identity but tmux session %s is alive",
                        agent.pid,
                        session_name,
                    )

            if live_info and not live_info.pane_dead and not pid_dead:
                continue
            child_sid = agent.child_session_id or agent.id
            if child_sid in self._recently_ended:
                continue

            logger.info(
                "Detected dead tmux pane for agent session=%s (tmux=%s)",
                child_sid,
                session_name,
            )

            # Look up the session to get external_id and source
            session = await asyncio.to_thread(self._lookup_session, child_sid)
            if session is None:
                logger.warning(
                    "Cannot synthesize session_end: session %s not found in DB", child_sid
                )
                self._recently_ended[child_sid] = now
                continue

            event = HookEvent(
                event_type=HookEventType.SESSION_END,
                session_id=session.external_id,
                source=(
                    parse_session_source(session.source) if session.source else SessionSource.CLAUDE
                ),
                timestamp=datetime.now(UTC),
                data={"cwd": None},
                metadata={
                    "_platform_session_id": session.id,
                    "_tmux_pane_death": True,
                },
            )

            try:
                await asyncio.to_thread(self._callback, event)
            except Exception:
                logger.exception("TmuxPaneMonitor: callback error for session %s", child_sid)

            self._recently_ended[child_sid] = now

    async def _check_attention_panes(self, *, active_runs: Sequence[AgentRun]) -> None:
        """Report attention for interactive panes without injecting input."""
        manager = self._attention_manager
        session_manager = self._session_manager
        if manager is None or session_manager is None:
            return

        try:
            sessions = await self._list_interactive_sessions()
        except Exception:
            logger.warning("TmuxPaneMonitor: failed to list interactive sessions", exc_info=True)
            return

        active_agent_sessions = {
            run.child_session_id for run in active_runs if run.child_session_id is not None
        }
        active_interactive_ids = {session.id for session in sessions}
        for attention in await asyncio.to_thread(manager.list_blocked):
            if (
                attention.run_id is None
                and attention.session_id is not None
                and attention.session_id not in active_interactive_ids
            ):
                await manager.transition_async(
                    asyncio.to_thread,
                    attention.entry_id,
                    state=None,
                    expected_attention_id=attention.attention_id,
                    expected_fingerprint=attention.fingerprint,
                )

        for session in sessions:
            if session.id in active_agent_sessions:
                continue
            terminal_context = session.terminal_context
            if not isinstance(terminal_context, Mapping):
                await self._clear_attention_if_current(session_attention_entry_id(session.id))
                continue
            pane_id = terminal_context.get("tmux_pane")
            if not isinstance(pane_id, str) or not pane_id:
                await self._clear_attention_if_current(session_attention_entry_id(session.id))
                continue
            try:
                from gobby.storage.terminals import TerminalManager

                if self._session_manager is None:
                    await self._clear_attention_if_current(session_attention_entry_id(session.id))
                    continue
                row = TerminalManager(self._session_manager.db).get_live_for_session(session.id)
                if row is None:
                    await self._clear_attention_if_current(session_attention_entry_id(session.id))
                    continue
                snapshot = await self._runtime.snapshot(row, 15)
                pane_output = snapshot.text
            except TimeoutError as exc:
                logger.debug(
                    "TmuxPaneMonitor: interactive pane capture timed out",
                    extra={
                        "pane_id": pane_id,
                        "session_id": session.id,
                        "provider": session.source or "",
                        "error": str(exc),
                    },
                )
                continue
            except Exception:
                logger.warning(
                    "TmuxPaneMonitor: failed to capture interactive pane %s",
                    pane_id,
                    exc_info=True,
                )
                continue
            if pane_output is None:
                continue
            await self._sync_interactive_attention(
                session.id,
                session.source or "",
                pane_output,
            )

    async def _sync_interactive_attention(
        self,
        session_id: str,
        provider: str,
        pane_output: str,
    ) -> None:
        manager = self._attention_manager
        if manager is None:
            return
        prompt_detector = self._prompt_detector.for_provider(provider)
        stall_classifier = self._stall_classifier.for_provider(provider)
        reason: PromptKind | None = None
        kind: AttentionKind | None = None
        classification_reason: str | None = None
        detected = prompt_detector.detect_prompt(pane_output)
        if detected is not None:
            reason = detected.kind
            kind = "actionable"
        else:
            classification = stall_classifier.classify(session_id, pane_output=pane_output)
            if classification.status is StallStatus.PROVIDER_STALL:
                reason = "stall"
                kind = "non_actionable"
                classification_reason = classification.reason

        if reason is None or kind is None:
            await self._clear_attention_if_current(session_attention_entry_id(session_id))
            return
        prompt_payload = (
            detected
            if detected is not None and detected.kind == reason
            else prompt_detector.classification_payload(
                kind=reason,
                label=classification_reason or reason,
            )
        )
        await manager.transition_async(
            asyncio.to_thread,
            session_attention_entry_id(session_id),
            state="blocked",
            session_id=session_id,
            reason=reason,
            kind=kind,
            fingerprint=prompt_payload.fingerprint,
            payload=prompt_payload.to_payload(),
        )

    async def _clear_attention_if_current(self, entry_id: str) -> None:
        manager = self._attention_manager
        if manager is None:
            return
        current = await asyncio.to_thread(manager.get, entry_id)
        if current is None or current.state is None:
            return
        await manager.transition_async(
            asyncio.to_thread,
            entry_id,
            state=None,
            expected_attention_id=current.attention_id,
            expected_fingerprint=current.fingerprint,
        )

    async def _list_active_runs(
        self,
        manager: LocalAgentRunManager,
    ) -> list[AgentRun]:
        runs: list[AgentRun] = []
        offset = 0
        while True:
            page = await asyncio.to_thread(
                manager.list_active_for_machine,
                require_machine_id(),
                limit=_AGENT_RUN_PAGE_SIZE,
                offset=offset,
            )
            runs.extend(page)
            if len(page) < _AGENT_RUN_PAGE_SIZE:
                return runs
            offset += len(page)

    async def _list_interactive_sessions(self) -> list[Session]:
        session_manager = self._session_manager
        if session_manager is None:
            return []
        sessions: list[Session] = []
        cursor_updated_at: str | None = None
        cursor_id: str | None = None
        while True:
            page = await asyncio.to_thread(
                session_manager.list,
                statuses=["active", "paused"],
                modes=["interactive"],
                limit=_INTERACTIVE_SESSION_PAGE_SIZE,
                cursor_updated_at=cursor_updated_at,
                cursor_id=cursor_id,
            )
            sessions.extend(page)
            if len(page) < _INTERACTIVE_SESSION_PAGE_SIZE:
                return sessions
            cursor_updated_at = page[-1].updated_at.isoformat()
            cursor_id = page[-1].id

    def _lookup_session(self, session_id: str) -> Session | None:
        """Look up a session from the database."""
        if not self._session_manager:
            logger.debug("No _session_manager configured, cannot look up %s", session_id)
            return None
        try:
            return self._session_manager.get(session_id)
        except Exception:
            logger.debug("Failed to look up session %s", session_id, exc_info=True)
            return None
