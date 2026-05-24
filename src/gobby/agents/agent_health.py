from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.agents.kill import kill_agent
from gobby.agents.stall_classifier import StallStatus
from gobby.utils.datetime import parse_stored_datetime

if TYPE_CHECKING:
    from gobby.agents.agent_cleanup import AgentCleanupHandler
    from gobby.agents.stall_classifier import StallClassifier
    from gobby.agents.tmux.session_manager import TmuxSessionManager
    from gobby.config.tmux import TmuxConfig
    from gobby.storage.agents import AgentRun, LocalAgentRunManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


class AgentHealthMonitor:
    """Handles periodic health checks for agents (unhealthy, init-timeout, stalls)."""

    def __init__(
        self,
        agent_run_manager: LocalAgentRunManager,
        db: HubDatabase,
        tmux: TmuxSessionManager,
        get_session_manager: Callable[[], SessionManager | None],
        stall_classifier: StallClassifier,
        cleanup_handler: AgentCleanupHandler,
        tmux_config: TmuxConfig,
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._agent_run_manager = agent_run_manager
        self._db = db
        self._tmux = tmux
        self._get_session_manager = get_session_manager
        self._stall_classifier = stall_classifier
        self._cleanup_handler = cleanup_handler
        self._tmux_config = tmux_config
        self._run_db_callback = run_db

    async def _run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db_callback is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db_callback(func, *args, **kwargs)

    async def _clear_tmux_session_name(self, run: AgentRun) -> None:
        if run.tmux_session_name:
            await self._run_db(
                self._agent_run_manager.clear_tmux_session_name,
                run.id,
                run.tmux_session_name,
            )

    async def check_unhealthy_agents(self) -> int:
        """Detect and clean up dead or expired agents."""
        runs = await self._run_db(self._agent_run_manager.list_active)
        now = datetime.now(UTC)
        cleaned = 0

        for run in runs:
            try:
                reason: str | None = None
                is_timeout = False

                if run.timeout_seconds and run.started_at:
                    started = parse_stored_datetime(run.started_at)
                    if started is None:
                        continue
                    age = (now - started).total_seconds()
                    if age > run.timeout_seconds:
                        reason = f"Agent exceeded {run.timeout_seconds}s timeout"
                        is_timeout = True
                        logger.info(
                            f"Agent {run.id} exceeded timeout ({age:.1f}s > {run.timeout_seconds}s)"
                        )

                if reason is None and run.tmux_session_name:
                    tmux_alive = await self._tmux.has_session(run.tmux_session_name)
                    if tmux_alive:
                        if run.pid:
                            try:
                                os.kill(run.pid, 0)
                            except ProcessLookupError:
                                reason = (
                                    f"PID {run.pid} dead but tmux '{run.tmux_session_name}' alive"
                                )
                                logger.info(f"Agent {run.id} {reason} - cleaning up")
                            except PermissionError:
                                pass
                    else:
                        reason = "tmux session died unexpectedly"
                        logger.info(
                            f"Detected dead tmux session '{run.tmux_session_name}' "
                            f"for agent {run.id}"
                        )

                if reason is None:
                    continue

                pane_snapshot = ""
                if run.tmux_session_name:
                    try:
                        pane_snapshot = (
                            await self._tmux.capture_pane(run.tmux_session_name, lines=50) or ""
                        )
                    except Exception as e:
                        logger.debug(f"Failed to capture pane for agent {run.id}: {e}")

                if run.tmux_session_name:
                    result = await kill_agent(
                        run,
                        self._db,
                        signal_name="TERM",
                        timeout=5.0,
                        close_terminal=True,
                    )
                    if result.get("success"):
                        await self._clear_tmux_session_name(run)
                elif run.pid:
                    try:
                        os.kill(run.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    except Exception as e:
                        logger.warning(f"Failed to kill process {run.pid}: {e}")

                error_msg = reason
                if pane_snapshot:
                    error_msg += f"\n\n--- Last terminal output ---\n{pane_snapshot[-2000:]}"

                await self._cleanup_handler.cleanup_agent(
                    run,
                    terminal_payload=error_msg,
                    is_timeout=is_timeout,
                )
                cleaned += 1

            except Exception as e:
                logger.warning(f"Error checking agent {run.id}: {e}")

        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} unhealthy agent(s)")

        return cleaned

    async def check_initialization_timeout(self) -> int:
        """Detect agents that never initialized (provider hung on connect)."""
        runs = await self._run_db(self._agent_run_manager.list_active)
        now = datetime.now(UTC)
        killed = 0
        session_manager = self._get_session_manager()

        for run in runs:
            if not run.started_at:
                continue
            try:
                started = parse_stored_datetime(run.started_at)
                if started is None:
                    continue
                age = (now - started).total_seconds()
                if age < self._tmux_config.init_timeout_seconds:
                    continue

                session_id = run.child_session_id or run.parent_session_id
                if not session_id or not session_manager:
                    continue

                session = await self._run_db(session_manager.get, session_id)
                if not session or not session.updated_at or not session.created_at:
                    continue

                updated = parse_stored_datetime(session.updated_at)
                created = parse_stored_datetime(session.created_at)
                if updated is None or created is None:
                    continue
                if (
                    updated - created
                ).total_seconds() > self._tmux_config.init_activity_grace_seconds:
                    continue

                logger.warning(
                    f"Agent {run.id} never initialized after {age:.0f}s "
                    f"(provider={run.provider}) — killing for provider rotation"
                )
                if run.tmux_session_name:
                    await self._tmux.kill_session(run.tmux_session_name)
                    await self._clear_tmux_session_name(run)
                elif run.pid:
                    try:
                        os.kill(run.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

                error_msg = (
                    f"Provider connection timed out: agent never initialized "
                    f"after {age:.0f}s (provider={run.provider})"
                )
                await self._cleanup_handler.cleanup_agent(run, terminal_payload=error_msg)
                killed += 1

            except Exception as e:
                logger.warning(f"Error checking init timeout for agent {run.id}: {e}")

        if killed > 0:
            logger.info(f"Killed {killed} uninitialized agent(s) for provider rotation")

        return killed

    async def check_provider_stalls(self) -> int:
        """Check tmux agents for provider-side stalls (rate limits, outages)."""
        runs = await self._run_db(self._get_active_terminal_runs)

        stalled = 0
        for run in runs:
            try:
                tmux_name = run.tmux_session_name
                if tmux_name is None:
                    logger.warning(
                        "Skipping provider stall check for run %s: missing tmux name", run.id
                    )
                    continue

                pane_output = await self._tmux.capture_pane(tmux_name, lines=8)
                classification = self._stall_classifier.classify(
                    run.id,
                    pane_output=pane_output,
                )

                if classification.status == StallStatus.PROVIDER_STALL:
                    logger.warning(
                        "Provider stall confirmed for agent %s: %s "
                        "(consecutive=%s) - killing agent",
                        run.id,
                        classification.reason,
                        classification.consecutive_hits,
                    )

                    await self._tmux.kill_session(tmux_name)
                    await self._clear_tmux_session_name(run)

                    error_msg = (
                        f"Provider stall: {classification.reason} "
                        f"(provider={run.provider}, "
                        f"consecutive_hits={classification.consecutive_hits})"
                    )
                    await self._cleanup_handler.cleanup_agent(run, terminal_payload=error_msg)
                    stalled += 1
            except Exception as e:
                logger.warning(f"Error checking provider stall for agent {run.id}: {e}")

        return stalled

    def _get_active_terminal_runs(self) -> list[AgentRun]:
        """Get active terminal agent runs with tmux sessions from DB."""
        runs = self._agent_run_manager.list_active()
        return [r for r in runs if r.tmux_session_name]
