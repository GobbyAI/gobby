"""Durable reconciliation for local agent-run resources."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from gobby.agents.agent_cleanup import AgentCleanupHandler
from gobby.agents.capture import TerminationErrorCode, terminate_managed_runtime_async
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.storage.agents import (
    AgentRun,
    AgentRunTerminalReason,
    LocalAgentRunManager,
    TerminalAction,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import TaskDispatchMutexManager
from gobby.telemetry.instruments import inc_counter, observe_histogram

logger = logging.getLogger(__name__)

DISPATCH_MUTEX_REFRESH_TTL_SECONDS = 600
DISPATCH_MUTEX_REFRESH_BATCH_SIZE = 100


def has_dispatch_stage_context(run: AgentRun) -> bool:
    """Return whether resume metadata carries dispatcher stage state."""
    try:
        metadata = run.resume_metadata_json
        if not isinstance(metadata, dict):
            return False
        if isinstance(metadata.get("stage_name"), str) and isinstance(
            metadata.get("stage_state"), str
        ):
            return True
        initial_variables = metadata.get("initial_variables")
        return isinstance(initial_variables, dict) and (
            isinstance(initial_variables.get("stage_name"), str)
            and isinstance(initial_variables.get("stage_state"), str)
        )
    except Exception:
        return False


class LifecycleReconciliation:
    """Re-drive interrupted termination and dispatch-lease state."""

    def __init__(
        self,
        *,
        agent_run_manager: LocalAgentRunManager,
        db: HubDatabase,
        tmux: TmuxSessionManager | None = None,
        cleanup_handler: AgentCleanupHandler,
        run_db: Callable[..., Awaitable[Any]],
        terminal_manager: Any | None = None,
        runtime_registry: Any | None = None,
        spawn_in_doubt_seconds: float = 150.0,
    ) -> None:
        self._agent_run_manager = agent_run_manager
        self._db = db
        self._tmux = tmux
        self._cleanup_handler = cleanup_handler
        self._run_db = run_db
        self._dispatch_refresh_cursor = 0
        self._terminal_manager = terminal_manager
        self._runtime_registry = runtime_registry
        self._spawn_in_doubt_seconds = spawn_in_doubt_seconds

    async def reconcile_pending_terminations(self, *, machine_id: str) -> int:
        """Re-drive interrupted capture, kill, and terminal sequences."""
        runs = await self._run_db(
            self._agent_run_manager.list_termination_candidates,
            machine_id=machine_id,
        )
        reconciled = 0
        for run in runs:
            if not run.terminal_id:
                logger.warning(
                    "Cannot reconcile termination for run %s without a terminal",
                    run.id,
                )
                continue

            action_value = run.pending_terminal_action
            if action_value in {"complete", "fail", "timeout", "cancel"}:
                action = cast("TerminalAction", action_value)
            elif run.tool_calls_count == 0 and run.turns_used == 0:
                action = "fail"
            else:
                action = "complete"
            reason = run.pending_terminal_reason
            if action == "fail" and not reason:
                reason = "Agent completed with no activity (0 tool calls, 0 turns)"

            async def terminalize(
                terminal_action: TerminalAction,
                payload: str | None,
                *,
                candidate: AgentRun = run,
            ) -> AgentRun | None:
                if terminal_action == "complete":
                    await self._cleanup_handler.terminalize_successful_run(
                        candidate.id,
                        notify_result={"status": "completed"},
                        message=f"Agent {candidate.id} completed",
                    )
                elif terminal_action == "cancel":
                    await self._cleanup_handler.terminalize_cancelled_run(
                        candidate.id,
                        terminal_reason=cast(
                            "AgentRunTerminalReason",
                            payload or "user_cancelled",
                        ),
                    )
                else:
                    await self._cleanup_handler.cleanup_agent(
                        candidate,
                        terminal_payload=payload or "Agent termination requested",
                        is_timeout=terminal_action == "timeout",
                    )
                return cast(
                    "AgentRun | None",
                    await self._run_db(self._agent_run_manager.get, candidate.id),
                )

            from gobby.terminals.lookup import active_terminal_for_run

            terminal = (
                None
                if self._terminal_manager is None
                else active_terminal_for_run(self._terminal_manager, run)
            )
            if terminal is None or self._runtime_registry is None:
                continue
            result = await terminate_managed_runtime_async(
                storage=self._agent_run_manager,
                run=run,
                terminal=terminal,
                runtime=self._runtime_registry.resolve(terminal.backend),
                action=action,
                reason=reason,
                terminalize=terminalize,
            )
            if result.success:
                reconciled += 1
            elif result.error_code == TerminationErrorCode.ALREADY_TERMINAL:
                # Not a failure: this pass exists to re-drive interrupted terminal
                # sequences, and the run finished its own. Every agent that calls
                # end_agent_run goes terminal between the candidate listing and
                # this call, so warning here fires on the healthy path and teaches
                # operators to ignore the logger that reports the retryable codes
                # (#20860). The other two callers of this result already skip it
                # the same way -- memory_watchdog and session_coordinator.
                logger.info(
                    "Termination reconciliation skipped for run %s: %s",
                    run.id,
                    result.error,
                )
            else:
                logger.warning(
                    "Termination reconciliation failed for run %s: %s (%s)",
                    run.id,
                    result.error,
                    result.error_code,
                )
        return reconciled

    async def reap_stale_pending(self) -> int:
        """Fail pending terminals older than the 2.3 in-doubt deadline."""
        manager = self._terminal_manager
        if manager is None:
            return 0
        stale = manager.list_stale_pending(self._spawn_in_doubt_seconds)
        reaped = 0
        for row in stale:
            failed = manager.fail_pending(row.id)
            if failed is not None:
                reaped += 1
        return reaped

    async def refresh_active_run_dispatch_mutexes(self, *, machine_id: str) -> int:
        """Extend or restore dispatch mutex leases for local active runs."""

        def refresh(start_cursor: int) -> tuple[int, int, int]:
            storage = TaskDispatchMutexManager(self._db)
            refreshed = 0
            skipped = 0
            runs = self._agent_run_manager.list_active_for_machine(
                machine_id,
                limit=DISPATCH_MUTEX_REFRESH_BATCH_SIZE,
                offset=start_cursor,
            )
            if not runs and start_cursor:
                start_cursor = 0
                runs = self._agent_run_manager.list_active_for_machine(
                    machine_id,
                    limit=DISPATCH_MUTEX_REFRESH_BATCH_SIZE,
                    offset=0,
                )
            for run in runs:
                if not run.task_id:
                    skipped += 1
                    continue
                mutex = storage.get_mutex(run.task_id)
                if mutex is not None:
                    if (
                        mutex.run_id == run.id
                        and mutex.lease_holder
                        and storage.refresh_mutex_for_run(
                            run.task_id,
                            run.id,
                            lease_holder=mutex.lease_holder,
                            ttl_seconds=DISPATCH_MUTEX_REFRESH_TTL_SECONDS,
                        )
                    ):
                        refreshed += 1
                    else:
                        skipped += 1
                    continue
                if not has_dispatch_stage_context(run):
                    skipped += 1
                    continue
                if storage.acquire_mutex(
                    run.task_id,
                    holder="dispatcher",
                    kind="heartbeat",
                    ttl_seconds=DISPATCH_MUTEX_REFRESH_TTL_SECONDS,
                    run_id=run.id,
                ):
                    refreshed += 1
                else:
                    skipped += 1
            next_cursor = (
                start_cursor + len(runs) if len(runs) == DISPATCH_MUTEX_REFRESH_BATCH_SIZE else 0
            )
            return refreshed, skipped, next_cursor

        try:
            start = time.perf_counter()
            refreshed, skipped, next_cursor = cast(
                tuple[int, int, int],
                await self._run_db(refresh, self._dispatch_refresh_cursor),
            )
            self._dispatch_refresh_cursor = next_cursor
            if refreshed:
                inc_counter("agent_lifecycle_dispatch_mutex_refreshed_runs_total", refreshed)
            if skipped:
                inc_counter("agent_lifecycle_dispatch_mutex_skipped_runs_total", skipped)
            observe_histogram(
                "agent_lifecycle_dispatch_mutex_refresh_seconds",
                time.perf_counter() - start,
            )
            return refreshed
        except Exception as error:
            logger.warning("Failed to refresh active run dispatch mutexes: %s", error)
            return 0
