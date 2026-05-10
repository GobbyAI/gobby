"""Pipeline heartbeat — safety net for event-driven pipeline execution.

Registered as a cron handler. On each tick:
1. Detects stalled RUNNING executions (no updated_at change)
2. Checks if associated agents are alive
3. Marks truly dead executions as FAILED
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from gobby.tasks.state_semantics import (
    ACTIVE_STAGE_STATES,
    get_claimed_session_id,
    is_task_actively_claimed,
)
from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.cron_models import CronJob
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks._manager import LocalTaskManager

logger = logging.getLogger(__name__)


def _submit_current_stage_for_review(
    task_manager: LocalTaskManager,
    task_id: str,
    stage_name: str,
) -> None:
    task_manager.stage_states.submit_for_review(
        task_id,
        stage_name,
        by_session_id=None,
    )


def _fail_current_stage(
    task_manager: LocalTaskManager,
    task_id: str,
    stage_name: str,
) -> None:
    task_manager.stage_states.fail_stage(
        task_id,
        stage_name,
        reason="stale_task_recovery",
        by_session_id=None,
    )


class PipelineHeartbeat:
    """Safety net for event-driven pipeline execution.

    Callable cron handler that detects stalled pipelines and marks
    dead executions as failed.
    """

    def __init__(
        self,
        execution_manager: LocalPipelineExecutionManager,
        agent_run_manager: LocalAgentRunManager,
        stall_threshold_seconds: float = 120.0,
        task_manager: LocalTaskManager | None = None,
        session_manager: SessionManager | None = None,
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._execution_manager = execution_manager
        self._agent_run_manager = agent_run_manager
        self._stall_threshold_seconds = stall_threshold_seconds
        self._task_manager = task_manager
        self._session_manager = session_manager
        self._run_db = run_db

    async def _run_sqlite(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db is None:
            import asyncio

            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db(func, *args, **kwargs)

    async def __call__(self, job: CronJob) -> str:
        """Cron handler entry point."""
        stalled = await self.check_stalled_executions()
        recovered = await self.check_stale_tasks()
        parts = [f"{stalled} stalled handled"]
        if recovered:
            parts.append(f"{recovered} stale tasks recovered")
        return f"Heartbeat: {', '.join(parts)}"

    async def check_stalled_executions(self) -> int:
        """Find stalled RUNNING executions and take corrective action.

        For each stalled execution:
        - If agents still alive → touch updated_at (slow, not stalled)
        - If agents dead → mark FAILED

        Returns:
            Number of stalled executions handled
        """
        stalled = await self._run_sqlite(
            self._execution_manager.get_stalled_executions,
            int(self._stall_threshold_seconds),
        )
        if not stalled:
            return 0

        handled = 0
        for execution in stalled:
            try:
                handled += await self._handle_stalled_execution(execution)
            except Exception:
                logger.error(f"Heartbeat error handling execution {execution.id}", exc_info=True)
        return handled

    async def _handle_stalled_execution(self, execution: PipelineExecution) -> int:
        """Handle a single stalled execution.

        Returns 1 if action was taken, 0 otherwise.
        """
        # Check if any agents are alive for this execution's session
        has_alive_agents = await self._run_sqlite(self._has_alive_agents, execution)

        if has_alive_agents:
            # Agents still working — touch updated_at so we don't re-flag
            await self._run_sqlite(
                self._execution_manager.update_execution_status,
                execution.id,
                ExecutionStatus.RUNNING,
            )
            logger.debug(
                f"Heartbeat: execution {execution.id} has alive agents, touched updated_at",
            )
            return 1

        # No alive agents — truly dead
        await self._run_sqlite(
            self._execution_manager.update_execution_status,
            execution.id,
            ExecutionStatus.FAILED,
            outputs_json=json.dumps({"error": "Heartbeat: execution stalled with no alive agents"}),
        )
        logger.warning(
            f"Heartbeat: marked execution {execution.id} as FAILED (stalled, no agents)",
        )
        return 1

    def _has_alive_agents(self, execution: PipelineExecution) -> bool:
        """Check if any agents are alive for a pipeline execution.

        Checks agent_runs DB table for active agents whose parent session
        matches the execution's session_id.
        """
        if not execution.session_id:
            return False
        try:
            runs = self._agent_run_manager.list_by_parent(execution.session_id)
            return len(runs) > 0
        except Exception:
            logger.exception(f"Failed to check alive agents for execution {execution.id}")
            return False

    def _is_session_alive(self, session_id: str) -> bool:
        """Check if a session is still alive.

        Interactive sessions (agent_depth == 0) are alive when active or paused
        (user is between prompts). Agent sessions (agent_depth > 0) are only
        alive when active — a paused agent session with no active run is dead.
        """
        if not self._session_manager:
            return False
        try:
            session = self._session_manager.get(session_id)
            if session is None:
                return False
            if session.status == "active":
                return True
            if session.status == "paused":
                # Agent sessions with no active run are dead (process exited)
                # Interactive sessions are alive (user is thinking)
                return getattr(session, "agent_depth", 0) == 0
            return False
        except Exception:
            logger.exception(f"Failed to check session liveness for {session_id}")
            return True  # Err on side of caution — assume alive

    async def check_stale_tasks(self) -> int:
        """Find claimed tasks with no alive agent or session and recover ownership.

        For each actively claimed task that has an owning session:
        1. Check if there's an active agent run (pending/running) for the task
        2. If not, check if the owning session is still alive
        3. If neither, recover the task based on its current stage

        Returns:
            Number of recovered tasks.
        """
        task_manager = self._task_manager
        agent_run_manager = self._agent_run_manager
        if not task_manager or not agent_run_manager:
            return 0

        try:
            active_claims = await self._run_sqlite(
                task_manager.list_tasks,
                current_stage_state=list(ACTIVE_STAGE_STATES),
                closed=False,
                limit=100,
            )
        except Exception:
            logger.exception("Heartbeat: failed to query claimed tasks")
            return 0

        recovered = 0
        for task in active_claims:
            owner_session_id = get_claimed_session_id(task)
            if not owner_session_id:
                continue
            if not is_task_actively_claimed(task, owner_session_id):
                continue
            try:
                has_active = await self._run_sqlite(
                    agent_run_manager.has_active_run_for_task, task.id
                )
                if has_active:
                    continue

                # No active agent run — check if the owning session is still alive.
                # Interactive CLI sessions don't create agent runs.
                session_alive = await self._run_sqlite(self._is_session_alive, owner_session_id)
                if session_alive:
                    continue

                # No active agent run and no live session — task ownership is orphaned.
                has_commits = bool(getattr(task, "commits", None))
                current_stage = await self._run_sqlite(
                    task_manager.stage_states.current_stage,
                    task.id,
                )
                if current_stage and current_stage.state == "in_progress" and has_commits:
                    await self._run_sqlite(
                        _submit_current_stage_for_review,
                        task_manager,
                        task.id,
                        current_stage.stage_name,
                    )
                    await self._run_sqlite(task_manager.release_task_claim, task.id)
                    logger.info(
                        "Heartbeat: submitted stale task %s (#%s) for review",
                        task.id,
                        task.seq_num,
                    )
                elif current_stage and current_stage.state == "in_progress":
                    await self._run_sqlite(
                        _fail_current_stage,
                        task_manager,
                        task.id,
                        current_stage.stage_name,
                    )
                    await self._run_sqlite(task_manager.release_task_claim, task.id)
                    logger.warning(
                        "Heartbeat: failed stale task %s (#%s) for retry",
                        task.id,
                        task.seq_num,
                    )
                else:
                    await self._run_sqlite(
                        task_manager.release_task_claim,
                        task.id,
                    )
                    logger.info(
                        "Heartbeat: released stale claim on task %s (#%s)",
                        task.id,
                        task.seq_num,
                    )
                recovered += 1
            except Exception:
                logger.exception(f"Heartbeat: error checking task {task.id} for staleness")
        return recovered
