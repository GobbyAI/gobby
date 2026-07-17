"""Pipeline heartbeat maintenance for the daemon-owned system automation loop.

On each maintenance tick:
1. Detects stalled RUNNING executions (no updated_at change)
2. Checks if associated agents are alive
3. Marks truly dead executions as FAILED
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, NamedTuple
from uuid import uuid4

from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.tasks.state_semantics import (
    ACTIVE_STAGE_STATES,
    get_claimed_session_id,
    is_task_actively_claimed,
)
from gobby.telemetry.health_metrics import record_automation_event
from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager, Task

logger = logging.getLogger(__name__)


class StaleTaskCheckResult(NamedTuple):
    recovered: int
    candidates: int


def _submit_current_stage_for_review(
    task_manager: LocalTaskManager,
    task_id: str,
    stage_name: str,
    preheld_mutex_run_id: str,
) -> None:
    task_manager.stage_states.submit_for_review(
        task_id,
        stage_name,
        by_session_id=None,
        preheld_mutex_run_id=preheld_mutex_run_id,
    )


def _recover_abandoned_stage(
    task_manager: LocalTaskManager,
    task_id: str,
    stage_name: str,
    preheld_mutex_run_id: str,
) -> None:
    task_manager.stage_states.recover_abandoned_stage(
        task_id,
        stage_name,
        reason="stale_task_recovery",
        by_session_id=None,
        preheld_mutex_run_id=preheld_mutex_run_id,
    )


def _recover_stale_task(
    task_manager: LocalTaskManager,
    task_id: str,
    owner_session_id: str,
) -> str | None:
    """Recover one stale task atomically under its dispatch mutex."""
    mutexes = TaskDispatchMutexManager(task_manager.db)
    lease_run_id = str(uuid4())
    lease_holder = f"pipeline_heartbeat:{lease_run_id}"
    if not mutexes.acquire_mutex(
        task_id,
        holder=lease_holder,
        kind="stale_task_recovery",
        ttl_seconds=30,
        run_id=lease_run_id,
    ):
        return None

    try:
        with task_manager.db.transaction():
            task = task_manager.get_task(task_id)
            if task is None or get_claimed_session_id(task) != owner_session_id:
                return None

            current_stage = task_manager.stage_states.current_stage(task_id)
            has_commits = bool(getattr(task, "commits", None))
            if current_stage and current_stage.state == "in_progress" and has_commits:
                _submit_current_stage_for_review(
                    task_manager,
                    task_id,
                    current_stage.stage_name,
                    lease_run_id,
                )
                action = "review"
            elif current_stage and current_stage.state == "in_progress":
                _recover_abandoned_stage(
                    task_manager,
                    task_id,
                    current_stage.stage_name,
                    lease_run_id,
                )
                action = "retry"
            else:
                action = "release"
            task_manager.release_task_claim(task_id)
        return action
    finally:
        mutexes.release_mutex(task_id, lease_holder)


class PipelineHeartbeat:
    """Maintenance service that detects stalled pipelines and stale task claims."""

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
        self._db_runner = run_db

    async def _run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._db_runner is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._db_runner(func, *args, **kwargs)

    async def check_stalled_executions(self) -> int:
        """Find stalled active executions and take corrective action.

        For each stalled execution:
        - If still PENDING → mark FAILED and release its dispatch mutex
        - If agents still alive → touch updated_at (slow, not stalled)
        - If agents dead → mark FAILED

        Returns:
            Number of stalled executions moved to a different status.
        """
        stalled = await self._run_db(
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
                logger.error("Heartbeat error handling execution %s", execution.id, exc_info=True)
        return handled

    async def _handle_stalled_execution(self, execution: PipelineExecution) -> int:
        """Handle a single stalled execution.

        Returns 1 if the execution status changed, 0 otherwise.
        """
        if execution.status == ExecutionStatus.PENDING:
            updated = await self._run_db(
                self._execution_manager.update_stalled_execution_status,
                execution.id,
                ExecutionStatus.FAILED,
                execution.status,
                execution.updated_at,
                outputs_json=json.dumps({"error": "Heartbeat: pipeline execution never started"}),
            )
            if updated is None:
                logger.info(
                    "Heartbeat: skipped pending execution %s because its state changed "
                    "since stall scan",
                    execution.id,
                )
                return 0
            logger.warning(
                "Heartbeat: marked execution %s as FAILED (never started)",
                execution.id,
            )
            record_automation_event("pipeline-heartbeat", "failed")
            return 1

        if not execution.session_id:
            logger.warning(
                "Heartbeat: skipped stalled execution %s because it has no owning session",
                execution.id,
            )
            return 0

        has_alive_agents = await self._run_db(self._has_alive_agents, execution)
        session_alive = False
        if self._session_manager:
            session_alive = await self._run_db(self._is_session_alive, execution.session_id)

        if has_alive_agents or session_alive:
            updated = await self._run_db(
                self._execution_manager.update_stalled_execution_status,
                execution.id,
                ExecutionStatus.RUNNING,
                execution.status,
                execution.updated_at,
            )
            if updated is None:
                logger.info(
                    "Heartbeat: skipped execution %s because its state changed since stall scan",
                    execution.id,
                )
                return 0
            logger.debug(
                "Heartbeat: execution %s is still alive, touched updated_at",
                execution.id,
            )
            return 0

        updated = await self._run_db(
            self._execution_manager.update_stalled_execution_status,
            execution.id,
            ExecutionStatus.FAILED,
            execution.status,
            execution.updated_at,
            outputs_json=json.dumps({"error": "Heartbeat: execution stalled with no alive agents"}),
        )
        if updated is None:
            logger.info(
                "Heartbeat: skipped execution %s because its state changed since stall scan",
                execution.id,
            )
            return 0
        logger.warning(
            "Heartbeat: marked execution %s as FAILED (stalled, no agents)", execution.id
        )
        record_automation_event("pipeline-heartbeat", "failed")
        return 1

    def _has_alive_agents(self, execution: PipelineExecution) -> bool:
        """Check if any agents are alive for a pipeline execution.

        Checks agent_runs DB table for active agents whose parent session
        matches the execution's session_id.
        """
        if not execution.session_id:
            return False
        runs = self._agent_run_manager.list_by_parent(execution.session_id)
        return len(runs) > 0

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
            logger.exception("Failed to check session liveness for %s", session_id)
            return True  # Err on side of caution — assume alive

    async def check_stale_tasks(self) -> StaleTaskCheckResult:
        """Find claimed tasks with no alive agent or session and recover ownership.

        For each actively claimed task that has an owning session:
        1. Check if there's an active agent run (pending/running) for the task
        2. If not, check if the owning session is still alive
        3. If neither, recover the task based on its current stage

        Returns:
            Recovered-task and scanned-candidate counts.
        """
        task_manager = self._task_manager
        agent_run_manager = self._agent_run_manager
        if not task_manager or not agent_run_manager:
            return StaleTaskCheckResult(recovered=0, candidates=0)

        candidates = await self._claimed_task_candidates()
        recovered = 0
        for task in candidates:
            owner_session_id = get_claimed_session_id(task)
            if owner_session_id is None:
                continue
            try:
                has_active = await self._run_db(agent_run_manager.has_active_run_for_task, task.id)
                if has_active:
                    continue

                # No active agent run — check if the owning session is still alive.
                # Interactive CLI sessions don't create agent runs.
                session_alive = await self._run_db(self._is_session_alive, owner_session_id)
                if session_alive:
                    continue

                # No active agent run and no live session — task ownership is orphaned.
                action = await self._run_db(
                    _recover_stale_task,
                    task_manager,
                    task.id,
                    owner_session_id,
                )
                if action == "review":
                    logger.info(
                        "Heartbeat: submitted stale task %s (#%s) for review",
                        task.id,
                        task.seq_num,
                    )
                elif action == "retry":
                    logger.info(
                        "Heartbeat: recovered abandoned task %s (#%s) for retry",
                        task.id,
                        task.seq_num,
                    )
                elif action == "release":
                    logger.info(
                        "Heartbeat: released stale claim on task %s (#%s)",
                        task.id,
                        task.seq_num,
                    )
                if action is not None:
                    recovered += 1
                    record_automation_event("pipeline-heartbeat", "recovered")
            except Exception:
                logger.exception("Heartbeat: error checking task %s for staleness", task.id)
        return StaleTaskCheckResult(recovered=recovered, candidates=len(candidates))

    async def count_running_executions(self) -> int:
        """Count running pipeline executions that need heartbeat monitoring."""
        return int(
            await self._run_db(
                self._execution_manager.count_executions,
                status=ExecutionStatus.RUNNING,
            )
        )

    async def _claimed_task_candidates(self) -> list[Task]:
        task_manager = self._task_manager
        agent_run_manager = self._agent_run_manager
        if not task_manager or not agent_run_manager:
            return []

        try:
            active_claims = await self._run_db(
                task_manager.list_tasks,
                current_stage_state=list(ACTIVE_STAGE_STATES),
                closed=False,
                limit=100,
                sort_by="updated_at",
                sort_order="asc",
            )
        except Exception:
            logger.exception("Heartbeat: failed to query claimed tasks")
            return []

        candidates: list[Task] = []
        for task in active_claims:
            owner_session_id = get_claimed_session_id(task)
            if not owner_session_id:
                continue
            if is_task_actively_claimed(task, owner_session_id):
                candidates.append(task)
        return candidates
