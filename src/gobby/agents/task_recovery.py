from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from gobby.agents.recovery_state import is_daemon_stop_parked
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._runtime_mutex import RuntimeDispatchMutex
from gobby.tasks.state_semantics import (
    current_stage,
    is_task_actively_claimed,
    projected_task_state,
)

logger = logging.getLogger(__name__)
RECOVERABLE_TERMINAL_STATUSES = ("error", "timeout", "cancelled")

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager


class _AgentRun(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def task_id(self) -> str | None: ...

    @property
    def child_session_id(self) -> str | None: ...

    @property
    def claimed_session_id(self) -> str | None: ...

    @property
    def provider(self) -> str: ...

    @property
    def error(self) -> str | None: ...

    @property
    def pid(self) -> int | None: ...

    @property
    def terminal_id(self) -> str | None: ...


class _AgentRunManager(Protocol):
    def get(self, run_id: str) -> _AgentRun | None: ...

    def list_by_status(
        self,
        status: str | None = ...,
        limit: int = ...,
        project_id: str | None = ...,
    ) -> Sequence[_AgentRun]: ...


class _StallClassifier(Protocol):
    def for_provider(self, provider_id: str) -> _StallClassifier: ...

    def is_provider_error(self, error_string: str | None) -> bool: ...

    def is_bootstrap_stall(self, error_string: str | None) -> bool: ...


class _Task(Protocol):
    id: str
    seq_num: int | None
    dispatch_failure_count: int | None


def _stage_name(stage: Any) -> str | None:
    if isinstance(stage, dict):
        raw = stage.get("stage_name") or stage.get("name")
    else:
        raw = getattr(stage, "stage_name", None) or getattr(stage, "name", None)
    return raw if isinstance(raw, str) and raw else None


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
        agent_run_manager: _AgentRunManager,
        stall_classifier: _StallClassifier,
        failure_threshold: int | None = None,
        terminal_agent_killer: Callable[[_AgentRun], Awaitable[dict[str, Any]]] | None = None,
        run_db: Callable[..., Awaitable[Any]] | None = None,
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
        self._terminal_agent_killer = terminal_agent_killer
        self._run_db_callback = run_db

    async def _run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db_callback is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db_callback(func, *args, **kwargs)

    async def resolve_claimed_task_for_run(self, db_run: _AgentRun) -> tuple[str, _Task] | None:
        """Resolve the task still owned by this run, if any."""
        if not self._task_manager:
            return None

        task_id = db_run.task_id
        owner_session_id = db_run.child_session_id

        if not task_id and owner_session_id:
            tasks = await self._run_db(
                self._task_manager.list_tasks,
                claimed_by_session_id=owner_session_id,
                closed=False,
            )
            if tasks:
                task_id = tasks[0].id

        if not task_id:
            return None

        task = await self._run_db(self._task_manager.get_task, task_id)
        if not task or not is_task_actively_claimed(task, owner_session_id):
            return None

        return task_id, task

    async def recover_task_from_terminal_agent(
        self,
        db_run: _AgentRun,
        *,
        outcome: Literal["failed", "cancelled"],
    ) -> bool:
        """Recover task ownership after a failed or cancelled agent run."""
        if not self._task_manager:
            return False
        try:
            resolved = await self.resolve_claimed_task_for_run(db_run)
            if resolved is None:
                if outcome == "cancelled" and db_run.task_id:
                    await self._run_db(
                        self._clear_claim_session_variables,
                        db_run,
                        db_run.task_id,
                    )
                return False

            task_id, task = resolved
            task_ref = f"#{task.seq_num}" if task.seq_num else task_id[:8]
            lifecycle_stage = projected_task_state(task)

            if not await self._verify_agent_dead_before_recovery(db_run, task_ref):
                return False

            if outcome == "cancelled":
                await self._release_dispatch_mutex_for_run(db_run)
                await self._run_db(
                    self._release_task_claim_with_mutex,
                    task_id,
                )
                await self._run_db(self._clear_claim_session_variables, db_run, task_id)
                logger.info(
                    "Recovered task %s after agent %s cancelled (status=%s)",
                    task_ref,
                    db_run.id,
                    lifecycle_stage,
                )
                return True

            is_provider = self._stall_classifier.for_provider(db_run.provider).is_provider_error(
                db_run.error
            )
            is_bootstrap_stall = self._is_bootstrap_stall(db_run.error)
            if is_provider:
                logger.info(
                    "Agent %s failed with provider error (provider=%s): %s",
                    db_run.id,
                    db_run.provider,
                    db_run.error,
                )

            if lifecycle_stage != "in_progress":
                await self._release_dispatch_mutex_for_run(db_run)
                await self._run_db(self._release_task_claim_with_mutex, task_id)
                await self._run_db(self._clear_claim_session_variables, db_run, task_id)
                logger.info(
                    "Released stale ownership on task %s after agent %s failed (status=%s)",
                    task_ref,
                    db_run.id,
                    lifecycle_stage,
                )
                return True

            await self._release_dispatch_mutex_for_run(db_run)
            failure_count = task.dispatch_failure_count or 0
            counts_dispatch_failure = (not is_provider) or is_bootstrap_stall
            if counts_dispatch_failure:
                failure_count += 1

            failure_reason = (
                "bootstrap_accounting_stall"
                if is_bootstrap_stall
                else "provider_startup_failed"
                if is_provider
                else "agent_run_failed"
            )
            if is_provider and not is_bootstrap_stall:
                await self._fail_current_stage(
                    task_id,
                    task,
                    reason=failure_reason,
                    by_session_id=db_run.child_session_id or db_run.claimed_session_id,
                )
                await self._run_db(
                    self._release_task_claim_with_mutex,
                    task_id,
                    dispatch_failure_count=failure_count,
                )
                await self._run_db(self._clear_claim_session_variables, db_run, task_id)
                logger.info(
                    "Released stale ownership on task %s after provider startup failure",
                    task_ref,
                )
                return True

            if counts_dispatch_failure and failure_count >= self._failure_threshold:
                await self._fail_current_stage(
                    task_id,
                    task,
                    reason=failure_reason,
                    by_session_id=db_run.child_session_id or db_run.claimed_session_id,
                )
                await self._run_db(
                    self._release_task_claim_with_mutex,
                    task_id,
                    dispatch_failure_count=0,
                    escalated_at=datetime.now(UTC).isoformat(),
                    escalation_reason=(
                        f"Bootstrap/accounting stalled {failure_count} dispatch attempts"
                        if is_bootstrap_stall
                        else f"Failed {failure_count} dispatch attempts"
                    ),
                )
                await self._run_db(self._clear_claim_session_variables, db_run, task_id)
                logger.warning(
                    "Task %s escalated after %s dispatch attempts",
                    task_ref,
                    failure_count,
                )
                return True

            await self._fail_current_stage(
                task_id,
                task,
                reason=failure_reason,
                by_session_id=db_run.child_session_id or db_run.claimed_session_id,
            )
            await self._run_db(
                self._release_task_claim_with_mutex,
                task_id,
                dispatch_failure_count=failure_count,
            )
            await self._run_db(self._clear_claim_session_variables, db_run, task_id)
            logger.info(
                "Recovered task %s to open after agent %s failed",
                task_ref,
                db_run.id,
            )
            return True
        except Exception as e:
            logger.warning("Failed to recover task for agent %s: %s", db_run.id, e)
            return False

    def _is_bootstrap_stall(self, error_string: str | None) -> bool:
        checker = getattr(self._stall_classifier, "is_bootstrap_stall", None)
        if not callable(checker):
            return False
        return checker(error_string) is True

    async def recover_tasks_from_terminal_agents(self, *, limit_per_status: int = 100) -> int:
        """Sweep terminal non-success runs whose task ownership was not recovered."""
        if not self._task_manager:
            return 0

        recovered = 0
        for status in RECOVERABLE_TERMINAL_STATUSES:
            runs = await self._run_db(
                self._agent_run_manager.list_by_status,
                status,
                limit_per_status,
            )
            outcome: Literal["failed", "cancelled"] = (
                "cancelled" if status == "cancelled" else "failed"
            )
            for db_run in runs:
                if is_daemon_stop_parked(db_run):
                    continue
                if await self.recover_task_from_terminal_agent(db_run, outcome=outcome):
                    recovered += 1
        return recovered

    async def _fail_current_stage(
        self,
        task_id: str,
        task: _Task,
        *,
        reason: str,
        by_session_id: str | None,
    ) -> None:
        """Return the active in-progress stage to ready through the stage state machine."""
        if not self._task_manager:
            return
        stage_name = _stage_name(current_stage(task))
        if stage_name is None:
            return
        try:
            await self._run_db(
                self._task_manager.stage_states.fail_stage,
                task_id,
                stage_name,
                reason=reason,
                by_session_id=by_session_id,
            )
        except Exception as exc:
            if exc.__class__.__name__ != "IllegalStageTransitionError":
                raise
            fresh = await self._run_db(
                self._task_manager.stage_states.get,
                task_id,
                stage_name,
            )
            if fresh is None or getattr(fresh, "state", None) != "ready":
                raise

    async def _verify_agent_dead_before_recovery(self, db_run: _AgentRun, task_ref: str) -> bool:
        # Terminal spawners persist the tmux pane PID, not the provider child PID.
        # Once the managed tmux target is cleared, that PID is stale and must not
        # be treated as a safe signal target.
        if db_run.terminal_id is None:
            return True
        if self._terminal_agent_killer is None:
            logger.warning(
                "Cannot recover task %s after agent %s without a terminal liveness verifier",
                task_ref,
                db_run.id,
            )
            return False

        result = await self._terminal_agent_killer(db_run)
        if result.get("success"):
            return True
        logger.warning(
            "Cannot recover task %s after agent %s; terminal kill/verify failed: %s",
            task_ref,
            db_run.id,
            result.get("error") or result.get("message"),
        )
        return False

    async def _release_dispatch_mutex_for_run(self, db_run: _AgentRun) -> None:
        if not self._task_manager:
            return
        db = getattr(self._task_manager, "db", None)
        if db is None:
            return
        cleared = await self._run_db(
            self._clear_dispatch_mutex_by_run_id,
            db,
            db_run.id,
        )
        if cleared:
            logger.info(
                "Released dispatch mutex for failed agent %s before task recovery",
                db_run.id,
            )

    @staticmethod
    def _clear_dispatch_mutex_by_run_id(db: Any, run_id: str) -> int:
        with db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM task_dispatch_mutex WHERE run_id = %s",
                (run_id,),
            )
            return int(cursor.rowcount)

    def _release_task_claim_with_mutex(self, task_id: str, **kwargs: Any) -> Any:
        if not self._task_manager:
            return None
        db = getattr(self._task_manager, "db", None)
        if db is None:
            return self._task_manager.release_task_claim(task_id, **kwargs)
        try:
            mutex = RuntimeDispatchMutex(
                TaskDispatchMutexManager(db),
                task_id,
                "task_recovery",
                "claim_release",
                30,
            )
        except TypeError as exc:
            logger.debug(
                "Skipping task recovery dispatch mutex for task %s: %s",
                task_id,
                exc,
            )
            return self._task_manager.release_task_claim(task_id, **kwargs)
        try:
            mutex.__enter__()
        except TypeError as exc:
            logger.debug(
                "Skipping task recovery dispatch mutex for task %s: %s",
                task_id,
                exc,
            )
            return self._task_manager.release_task_claim(task_id, **kwargs)
        try:
            return self._task_manager.release_task_claim(task_id, **kwargs)
        finally:
            mutex.__exit__(None, None, None)

    def _clear_claim_session_variables(self, db_run: _AgentRun, task_id: str) -> None:
        """Remove recovered task from any agent-owned session claim variables."""
        if not self._task_manager:
            return
        db = getattr(self._task_manager, "db", None)
        if db is None:
            return

        try:
            from gobby.workflows.state_manager import SessionVariableManager
            from gobby.workflows.task_claim_state import remove_claimed_task

            session_var_manager = SessionVariableManager(db)
            session_ids = {
                session_id
                for session_id in (db_run.child_session_id, db_run.claimed_session_id)
                if session_id
            }
            for session_id in session_ids:
                session_vars = session_var_manager.get_variables(session_id)
                merge_dict = remove_claimed_task(session_vars, task_id)
                session_var_manager.merge_existing_variables(session_id, merge_dict)
        except Exception as e:
            logger.debug(
                "Best-effort claimed_tasks cleanup failed for agent %s task %s: %s",
                db_run.id,
                task_id,
                e,
            )

    async def recover_task_from_failed_agent(self, run_id: str) -> None:
        """Recover task ownership after a failed agent run."""
        db_run = await self._run_db(self._agent_run_manager.get, run_id)
        if not db_run:
            return
        await self.recover_task_from_terminal_agent(db_run, outcome="failed")
