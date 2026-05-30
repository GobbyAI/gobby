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


def _session_counter(session: Any, name: str) -> int:
    value = getattr(session, name, 0)
    if isinstance(value, int) and not isinstance(value, bool):
        return max(value, 0)
    return 0


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

    async def _capture_pane_snapshot(self, run: AgentRun, *, lines: int = 50) -> str:
        if not run.tmux_session_name:
            return ""
        try:
            return await self._tmux.capture_pane(run.tmux_session_name, lines=lines) or ""
        except Exception as e:
            logger.debug(f"Failed to capture pane for agent {run.id}: {e}")
            return ""

    async def _bootstrap_accounting_stall_error(
        self,
        run: AgentRun,
        *,
        age_seconds: float,
        pane_snapshot: str,
    ) -> str | None:
        if not run.child_session_id:
            return None
        if (run.tool_calls_count or 0) > 0 or (run.turns_used or 0) > 0:
            return None
        if not pane_snapshot.strip():
            return None

        session_manager = self._get_session_manager()
        if session_manager is None:
            return None

        try:
            session = await self._run_db(session_manager.get, run.child_session_id)
        except Exception as e:
            logger.debug("Failed to read child session for agent %s: %s", run.id, e)
            return None
        if session is None:
            return None

        message_count = _session_counter(session, "message_count")
        turn_count = _session_counter(session, "turn_count")
        tool_call_count = _session_counter(session, "tool_call_count")
        if message_count or turn_count or tool_call_count:
            return None

        transcript_path = getattr(session, "transcript_path", None) or "missing"
        transcript_processed = bool(getattr(session, "transcript_processed", False))
        context_injected = bool(getattr(session, "context_injected", False))
        session_updated_at = getattr(session, "updated_at", None) or "unknown"
        session_created_at = getattr(session, "created_at", None) or "unknown"

        return (
            "Provider bootstrap/accounting stall: terminal output was visible but "
            "Gobby session accounting stayed at zero "
            f"after {age_seconds:.0f}s "
            f"(provider={run.provider}, model={run.model or 'unknown'}, "
            f"agent={run.agent_name or 'unknown'}, run_id={run.id}, "
            f"child_session_id={run.child_session_id}, "
            f"tmux_session={run.tmux_session_name or 'none'}, pid={run.pid or 'none'}, "
            f"context_injected={context_injected}, message_count={message_count}, "
            f"turn_count={turn_count}, tool_call_count={tool_call_count}, "
            f"transcript_path={transcript_path}, "
            f"transcript_processed={transcript_processed}, "
            f"session_created_at={session_created_at}, session_updated_at={session_updated_at})"
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
                timeout_age: float | None = None
                pane_snapshot = ""

                if run.timeout_seconds and run.started_at:
                    started = parse_stored_datetime(run.started_at)
                    if started is None:
                        continue
                    age = (now - started).total_seconds()
                    if age > run.timeout_seconds:
                        reason = f"Agent exceeded {run.timeout_seconds}s timeout"
                        is_timeout = True
                        timeout_age = age
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

                if run.tmux_session_name:
                    pane_snapshot = await self._capture_pane_snapshot(run, lines=50)

                if is_timeout and timeout_age is not None:
                    bootstrap_error = await self._bootstrap_accounting_stall_error(
                        run,
                        age_seconds=timeout_age,
                        pane_snapshot=pane_snapshot,
                    )
                    if bootstrap_error is not None:
                        reason = bootstrap_error
                        is_timeout = False
                        logger.warning(
                            "Agent %s hit bootstrap/accounting stall containment",
                            run.id,
                        )

                if run.tmux_session_name:
                    result = await kill_agent(
                        run,
                        self._db,
                        signal_name="TERM",
                        timeout=5.0,
                        close_terminal=True,
                    )
                    if not result.get("success"):
                        logger.warning(
                            "Skipping cleanup for run %s after failed terminal kill: %s",
                            run.id,
                            result.get("error") or result.get("message"),
                        )
                        continue
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
