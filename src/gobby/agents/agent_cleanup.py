from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from gobby.agents import terminal_delivery
from gobby.agents.terminal_cleanup import TerminalResourceCleaner

if TYPE_CHECKING:
    from gobby.agents.loop_tracker import LoopTracker
    from gobby.agents.prompt_detector import PromptDetector
    from gobby.agents.stall_classifier import StallClassifier
    from gobby.agents.task_recovery import TaskRecoveryHandler
    from gobby.agents.terminal_prompt_monitor import TerminalPromptMonitor
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.hooks.session_coordinator import SessionCoordinator
    from gobby.storage.agents import (
        AgentRun,
        AgentRunTerminalReason,
        LocalAgentRunManager,
        TerminalAction,
    )
    from gobby.storage.attention import AttentionStateManager
    from gobby.storage.clones import LocalCloneManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)
SESSION_STATS_LOOKUP_TIMEOUT_SECONDS = 2.0


class AgentCleanupHandler:
    """Coordinate terminal state transitions for agent runs."""

    def __init__(
        self,
        agent_run_manager: LocalAgentRunManager,
        db: HubDatabase,
        get_session_manager: Callable[[], SessionManager | None],
        get_session_coordinator: Callable[[], SessionCoordinator | None],
        clone_storage: LocalCloneManager | None,
        completion_registry: CompletionEventRegistry | None,
        task_recovery: TaskRecoveryHandler,
        prompt_detector: PromptDetector,
        terminal_prompt_monitor: TerminalPromptMonitor,
        stall_classifier: StallClassifier,
        loop_tracker: LoopTracker,
        master_fds: dict[str, int],
        kill_tmux_session: Callable[[str], Awaitable[bool]] | None = None,
        run_db: Callable[..., Awaitable[Any]] | None = None,
        attention_manager: AttentionStateManager | None = None,
        terminal_services: Any | None = None,
    ) -> None:
        self._agent_run_manager = agent_run_manager
        self._db = db
        self._get_session_manager = get_session_manager
        self._completion_registry = completion_registry
        self._task_recovery = task_recovery
        self._run_db_callback = run_db
        self._terminal_services = terminal_services
        self._resource_cleaner = TerminalResourceCleaner(
            agent_run_manager=agent_run_manager,
            db=db,
            get_session_coordinator=get_session_coordinator,
            clone_storage=clone_storage,
            completion_registry=completion_registry,
            prompt_detector=prompt_detector,
            terminal_prompt_monitor=terminal_prompt_monitor,
            stall_classifier=stall_classifier,
            loop_tracker=loop_tracker,
            master_fds=master_fds,
            kill_tmux_session=kill_tmux_session,
            run_db=self._run_db,
            attention_manager=attention_manager,
        )

    async def _run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db_callback is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db_callback(func, *args, **kwargs)

    async def notify_terminal_completion(
        self,
        run_id: str,
        *,
        result: dict[str, Any],
        message: str,
    ) -> None:
        """Notify waiters about a terminal run transition."""
        await terminal_delivery.deliver_and_cleanup_terminal_run(
            db=self._db,
            completion_registry=self._completion_registry,
            run_id=run_id,
            result=result,
            message=message,
            run_db=self._run_db,
        )

    async def post_terminal_cleanup(
        self,
        run: AgentRun,
        *,
        cleanup_session_id: str | None = None,
        allow_parent_session_fallback: bool = False,
        notification_result: dict[str, Any] | None = None,
        notification_message: str = "",
        force_full_cleanup: bool = False,
    ) -> None:
        """Release in-memory and isolation state for a terminal agent run."""
        await self._resource_cleaner.post_terminal_cleanup(
            run,
            cleanup_session_id=cleanup_session_id,
            allow_parent_session_fallback=allow_parent_session_fallback,
            notification_result=notification_result,
            notification_message=notification_message,
            force_full_cleanup=force_full_cleanup,
        )

    async def cleanup_terminal_tmux_sessions(self) -> int:
        """Close tmux sessions left behind for already-terminal agent runs."""
        return await self._resource_cleaner.cleanup_terminal_tmux_sessions()

    async def _completion_stats_for_run(self, run: AgentRun) -> tuple[int, int]:
        tool_calls_count = run.tool_calls_count or 0
        turns_used = run.turns_used or 0
        if not run.child_session_id or (tool_calls_count and turns_used):
            return tool_calls_count, turns_used

        session_manager = self._get_session_manager()
        if session_manager is None:
            return tool_calls_count, turns_used

        try:
            session = await asyncio.wait_for(
                self._run_db(session_manager.get, run.child_session_id),
                timeout=SESSION_STATS_LOOKUP_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.debug("Timed out reading session stats for agent %s", run.id)
            return tool_calls_count, turns_used
        except Exception:
            logger.debug("Failed to read session stats for agent %s", run.id, exc_info=True)
            return tool_calls_count, turns_used
        if session is None:
            return tool_calls_count, turns_used

        session_tool_calls = getattr(session, "tool_call_count", None)
        session_turns = getattr(session, "turn_count", None)
        if (
            isinstance(session_tool_calls, int)
            and not isinstance(session_tool_calls, bool)
            and tool_calls_count == 0
        ):
            tool_calls_count = session_tool_calls
        if (
            isinstance(session_turns, int)
            and not isinstance(session_turns, bool)
            and turns_used == 0
        ):
            turns_used = session_turns
        return tool_calls_count, turns_used

    async def _run_capture_policy(
        self,
        run: AgentRun,
        *,
        action: TerminalAction,
        reason: str | None,
        terminalize: Callable[[], Awaitable[AgentRun | None]],
    ) -> tuple[bool, AgentRun | None]:
        """Route a live managed tmux session through capture-before-kill.

        Returns (routed, terminal_run). routed=False means no live session
        needed the policy and the caller applies its direct transition —
        including when a policy invocation higher in the stack (reconciler,
        watchdog) already killed the session before invoking this terminalizer.
        """
        if run.status not in ("pending", "running"):
            return False, None
        from gobby.agents.capture import terminate_managed_runtime_async

        services = self._terminal_services
        terminal = None if services is None else services.terminal_for(run)
        if terminal is None or not await services.is_live(run):
            return False, None

        async def _terminalize(
            _action: TerminalAction,
            _payload: str | None,
        ) -> AgentRun | None:
            return await terminalize()

        result = await terminate_managed_runtime_async(
            storage=self._agent_run_manager,
            run=run,
            terminal=terminal,
            runtime=services.runtime_for(terminal),
            action=action,
            reason=reason,
            terminalize=_terminalize,
        )
        if not result.success:
            logger.warning(
                "Capture-policy terminalization failed for run %s: %s (%s)",
                run.id,
                result.error,
                result.error_code,
            )
            return True, None
        return True, result.run

    async def terminalize_successful_run(
        self,
        run_id: str,
        *,
        notify_result: dict[str, Any],
        message: str,
        completion_result: str | None = None,
        terminal_reason: AgentRunTerminalReason | None = None,
    ) -> bool:
        """Complete an active run through the cancellation-shielded delivery scope."""

        async def operation() -> bool:
            return await self._terminalize_successful_run_unshielded(
                run_id,
                notify_result=notify_result,
                message=message,
                completion_result=completion_result,
                terminal_reason=terminal_reason,
            )

        return bool(await terminal_delivery.shielded_terminal_delivery(run_id, operation))

    async def _terminalize_successful_run_unshielded(
        self,
        run_id: str,
        *,
        notify_result: dict[str, Any],
        message: str,
        completion_result: str | None = None,
        terminal_reason: AgentRunTerminalReason | None = None,
    ) -> bool:
        """Complete an active run, notify subscribers, and clean child-owned state.

        Args:
            run_id: Agent run id to mark complete.
            notify_result: Payload delivered to completion subscribers.
            message: Completion message delivered alongside the payload.
            completion_result: Optional final result to persist instead of the
                run's current result.
            terminal_reason: Optional reason for successful terminalization.

        Returns:
            True when the run transitioned to complete; False when the run was
            already terminal or missing after cleanup reconciliation.
        """
        current = await self._run_db(self._agent_run_manager.get, run_id)
        if current is None:
            logger.debug("Successful terminalization no-op for missing run %s", run_id)
            return False

        transitioned_here = False

        async def _complete_run() -> AgentRun | None:
            nonlocal transitioned_here
            tool_calls_count, turns_used = await self._completion_stats_for_run(current)
            completed = cast(
                "AgentRun | None",
                await self._run_db(
                    self._agent_run_manager.complete,
                    run_id,
                    result=completion_result,
                    tool_calls_count=tool_calls_count,
                    turns_used=turns_used,
                    terminal_reason=terminal_reason,
                ),
            )
            transitioned_here = completed is not None
            return completed

        routed, db_run = await self._run_capture_policy(
            current,
            action="complete",
            reason=None,
            terminalize=_complete_run,
        )
        if routed and db_run is None:
            return False
        if not routed:
            db_run = await _complete_run()
        if db_run is not None and not transitioned_here:
            db_run = None
        if db_run is None:
            latest = await self._run_db(self._agent_run_manager.get, run_id)
            logger.debug(
                "Successful terminalization no-op for run %s; current status=%s",
                run_id,
                latest.status if latest else "missing",
            )
            if latest is not None:
                await self.post_terminal_cleanup(
                    latest,
                    cleanup_session_id=latest.child_session_id,
                    allow_parent_session_fallback=False,
                )
            return False

        await self.post_terminal_cleanup(
            db_run,
            cleanup_session_id=db_run.child_session_id,
            allow_parent_session_fallback=False,
            notification_result=notify_result,
            notification_message=message,
        )
        return True

    async def terminalize_cancelled_run(
        self,
        run_id: str,
        *,
        terminal_reason: AgentRunTerminalReason,
    ) -> bool:
        """Cancel an active run through the cancellation-shielded delivery scope."""

        async def operation() -> bool:
            return await self._terminalize_cancelled_run_unshielded(
                run_id,
                terminal_reason=terminal_reason,
            )

        return bool(await terminal_delivery.shielded_terminal_delivery(run_id, operation))

    async def _terminalize_cancelled_run_unshielded(
        self,
        run_id: str,
        *,
        terminal_reason: AgentRunTerminalReason,
    ) -> bool:
        """Mark an active run cancelled, recover ownership, and notify waiters."""
        current = await self._run_db(self._agent_run_manager.get, run_id)
        if current is None:
            logger.debug("Cancelled terminalization no-op for missing run %s", run_id)
            return False

        transitioned_here = False

        async def _cancel_run() -> AgentRun | None:
            nonlocal transitioned_here
            cancelled = cast(
                "AgentRun | None",
                await self._run_db(
                    self._agent_run_manager.cancel,
                    run_id,
                    terminal_reason=terminal_reason,
                ),
            )
            transitioned_here = cancelled is not None
            return cancelled

        routed, db_run = await self._run_capture_policy(
            current,
            action="cancel",
            reason=terminal_reason,
            terminalize=_cancel_run,
        )
        if routed and db_run is None:
            return False
        if not routed:
            db_run = await _cancel_run()
        if db_run is not None and not transitioned_here:
            db_run = None
        if db_run is None:
            latest = await self._run_db(self._agent_run_manager.get, run_id)
            logger.debug(
                "Cancelled terminalization no-op for run %s; current status=%s",
                run_id,
                latest.status if latest else "missing",
            )
            return False

        if terminal_reason != "daemon_stop":
            await self._task_recovery.recover_task_from_terminal_agent(
                db_run,
                outcome="cancelled",
            )
        await self.post_terminal_cleanup(
            db_run,
            cleanup_session_id=db_run.child_session_id,
            allow_parent_session_fallback=False,
            notification_result={
                "status": "cancelled",
                "terminal_reason": terminal_reason,
                "run_id": db_run.id,
            },
            notification_message=f"Agent {db_run.id} cancelled",
        )
        from gobby.build.dispatch_tick import schedule_dispatcher_tick_for_task

        if db_run.task_id:
            try:
                schedule_dispatcher_tick_for_task(
                    self._db,
                    task_id=db_run.task_id,
                    reason="agent_parked"
                    if terminal_reason == "daemon_stop"
                    else "agent_cancelled",
                )
            except Exception:
                logger.warning(
                    "Failed to schedule dispatcher tick for cancelled agent run %s",
                    db_run.id,
                    exc_info=True,
                )
        return True

    async def expire_terminal_run_sessions(self) -> int:
        """Expire sessions whose agent run is already in a terminal state."""
        expired = await self._run_db(self._agent_run_manager.expire_sessions_for_terminal_runs)
        if expired:
            logger.info("Expired %s session(s) for terminal agent runs", expired)
        closed = await self.cleanup_terminal_tmux_sessions()
        if closed:
            logger.info("Closed %s lingering tmux session(s) for terminal agent runs", closed)
        return cast(int, expired)

    async def cleanup_stale_pending_runs(self, *, machine_id: str) -> int:
        """Clean up agent runs stuck in pending status after daemon restart."""
        run_ids = await self.run_acknowledged_stale_sweeps(
            machine_id=machine_id,
            pending_timeout_minutes=60,
        )
        return len(run_ids)

    async def run_acknowledged_stale_sweeps(
        self,
        *,
        machine_id: str,
        running_timeout_minutes: int | None = None,
        pending_timeout_minutes: int | None = None,
        pending_long_timeout_minutes: int = 1440,
    ) -> list[str]:
        """Transition stale runs and await subscriber delivery for every returned ID."""

        async def operation() -> list[str]:
            transitioned: list[str] = []
            if running_timeout_minutes is not None:
                transitioned.extend(
                    await self._run_db(
                        self._agent_run_manager.cleanup_stale_runs,
                        machine_id=machine_id,
                        default_timeout_minutes=running_timeout_minutes,
                    )
                )
            if pending_timeout_minutes is not None:
                transitioned.extend(
                    await self._run_db(
                        self._agent_run_manager.cleanup_stale_pending_runs,
                        machine_id=machine_id,
                        timeout_minutes=pending_timeout_minutes,
                        long_timeout_minutes=pending_long_timeout_minutes,
                    )
                )

            run_ids = list(dict.fromkeys(transitioned))
            for run_id in run_ids:
                delivered = await terminal_delivery.deliver_existing_terminal_run_in_scope(
                    db=self._db,
                    agent_run_manager=self._agent_run_manager,
                    completion_registry=self._completion_registry,
                    run_id=run_id,
                    run_db=self._run_db,
                )
                if not delivered:
                    logger.warning(
                        "Stale sweep returned non-terminal agent run %s; retaining subscribers",
                        run_id,
                    )
            return run_ids

        return list(
            await terminal_delivery.shielded_terminal_delivery("stale-sweeps", operation) or [],
        )

    async def cleanup_agent(
        self,
        run: AgentRun,
        terminal_payload: str,
        is_success: bool = False,
        is_timeout: bool = False,
    ) -> None:
        """Full cleanup chain for an agent that needs cleanup.

        ``terminal_payload`` is stored as the success result or the failure/
        timeout error, depending on the terminal transition.
        """
        terminal_run = run
        transitioned = False
        notification_result: dict[str, str] | None = None
        notification_message = ""

        if run.status in ("pending", "running"):
            tool_calls_count, turns_used = await self._completion_stats_for_run(run)
            if is_success:
                updated = await self._run_db(
                    self._agent_run_manager.complete,
                    run.id,
                    result=terminal_payload,
                    tool_calls_count=tool_calls_count,
                    turns_used=turns_used,
                )
                if updated is not None:
                    terminal_run = updated
                    transitioned = True
            elif is_timeout:
                updated = await self._run_db(
                    self._agent_run_manager.timeout,
                    run.id,
                    error=terminal_payload,
                    tool_calls_count=tool_calls_count,
                    turns_used=turns_used,
                )
                if updated is not None:
                    terminal_run = updated
                    transitioned = True
                    logger.info(
                        "Marked agent run %s as timed out: %s",
                        run.id,
                        terminal_payload,
                    )
            else:
                updated = await self._run_db(
                    self._agent_run_manager.fail,
                    run.id,
                    error=terminal_payload,
                    tool_calls_count=tool_calls_count,
                    turns_used=turns_used,
                )
                if updated is not None:
                    terminal_run = updated
                    transitioned = True
                    logger.info(
                        "Marked agent run %s as failed: %s",
                        run.id,
                        terminal_payload,
                    )

        if transitioned:
            if not is_success:
                await self._task_recovery.recover_task_from_terminal_agent(
                    terminal_run, outcome="failed"
                )

            if is_success:
                notification_result = {"status": "completed"}
            else:
                notification_result = {"status": "error", "error": terminal_payload}
            notification_message = f"Agent {run.id} {'completed' if is_success else 'failed'}"
        else:
            current = await self._run_db(self._agent_run_manager.get, run.id)
            logger.debug(
                "Terminal cleanup no-op for run %s; current status=%s",
                run.id,
                current.status if current else "missing",
            )
            if current is not None:
                terminal_run = current

        await self.post_terminal_cleanup(
            terminal_run,
            cleanup_session_id=terminal_run.child_session_id,
            allow_parent_session_fallback=False,
            notification_result=notification_result,
            notification_message=notification_message,
        )
