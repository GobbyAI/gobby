from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from gobby.tasks.state_semantics import is_task_actively_claimed, projected_task_state

if TYPE_CHECKING:
    from gobby.agents.stall_classifier import StallClassifier
    from gobby.storage.agents import AgentRun, LocalAgentRunManager
    from gobby.storage.tasks import LocalTaskManager, Task

logger = logging.getLogger(__name__)


def get_task_failure_threshold() -> int:
    """Return the configured task failure escalation threshold."""
    raw = os.environ.get("GOBBY_TASK_FAILURE_THRESHOLD", "3")
    try:
        threshold = int(raw)
    except ValueError as exc:
        raise ValueError("GOBBY_TASK_FAILURE_THRESHOLD must be a positive integer") from exc
    if threshold < 1:
        raise ValueError("GOBBY_TASK_FAILURE_THRESHOLD must be a positive integer")
    return threshold


class TaskRecoveryHandler:
    """Handles task ownership recovery for failed or cancelled agent runs."""

    def __init__(
        self,
        task_manager: LocalTaskManager | None,
        agent_run_manager: LocalAgentRunManager,
        stall_classifier: StallClassifier,
        failure_threshold: int | None = None,
    ) -> None:
        failure_threshold = (
            get_task_failure_threshold() if failure_threshold is None else failure_threshold
        )
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be a positive integer")
        self._task_manager = task_manager
        self._agent_run_manager = agent_run_manager
        self._stall_classifier = stall_classifier
        self._failure_threshold = failure_threshold

    async def resolve_claimed_task_for_run(self, db_run: AgentRun) -> tuple[str, Task] | None:
        """Resolve the task still owned by this run, if any."""
        if not self._task_manager:
            return None

        task_id = db_run.task_id

        if not task_id and db_run.child_session_id:
            tasks = await asyncio.to_thread(
                self._task_manager.list_tasks,
                claimed_by_session_id=db_run.child_session_id,
                closed=False,
            )
            if tasks:
                task_id = tasks[0].id

        if not task_id:
            return None

        task = await asyncio.to_thread(self._task_manager.get_task, task_id)
        expected_owner = db_run.child_session_id or db_run.claimed_session_id
        if not task or not is_task_actively_claimed(task, expected_owner):
            return None

        return task_id, task

    async def recover_task_from_terminal_agent(
        self,
        db_run: AgentRun,
        *,
        outcome: Literal["failed", "cancelled"],
    ) -> None:
        """Recover task ownership after a failed or cancelled agent run."""
        if not self._task_manager:
            return
        try:
            resolved = await self.resolve_claimed_task_for_run(db_run)
            if resolved is None:
                return

            task_id, task = resolved
            task_ref = f"#{task.seq_num}" if task.seq_num else task_id[:8]
            lifecycle_stage = projected_task_state(task)

            if outcome == "cancelled":
                await asyncio.to_thread(
                    self._task_manager.release_task_claim,
                    task_id,
                )
                logger.info(
                    "Recovered task %s after agent %s cancelled (status=%s)",
                    task_ref,
                    db_run.id,
                    lifecycle_stage,
                )
                return

            is_provider = self._stall_classifier.is_provider_error(db_run.error)
            if is_provider:
                logger.info(
                    "Agent %s failed with provider error (provider=%s): %s",
                    db_run.id,
                    db_run.provider,
                    db_run.error,
                )

            if lifecycle_stage != "in_progress":
                await asyncio.to_thread(self._task_manager.release_task_claim, task_id)
                logger.info(
                    "Released stale ownership on task %s after agent %s failed (status=%s)",
                    task_ref,
                    db_run.id,
                    lifecycle_stage,
                )
                return

            failure_count = task.dispatch_failure_count or 0
            if not is_provider:
                failure_count += 1

            if not is_provider and failure_count >= self._failure_threshold:
                await asyncio.to_thread(
                    self._task_manager.release_task_claim,
                    task_id,
                    dispatch_failure_count=0,
                    escalated_at=datetime.now(UTC).isoformat(),
                    escalation_reason=f"Failed {failure_count} times across different agents",
                )
                logger.warning(
                    "Task %s escalated: %s failures across different agents",
                    task_ref,
                    failure_count,
                )
                return

            await asyncio.to_thread(
                self._task_manager.release_task_claim,
                task_id,
                dispatch_failure_count=failure_count,
            )
            logger.info(
                "Recovered task %s to open after agent %s failed",
                task_ref,
                db_run.id,
            )
        except Exception as e:
            logger.warning("Failed to recover task for agent %s: %s", db_run.id, e)

    async def recover_task_from_failed_agent(self, run_id: str) -> None:
        """Recover task ownership after a failed agent run."""
        db_run = await asyncio.to_thread(self._agent_run_manager.get, run_id)
        if not db_run:
            return
        await self.recover_task_from_terminal_agent(db_run, outcome="failed")
