"""Cleanup helpers for terminal agent runtime state."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.workflows.state_manager import WorkflowInstanceManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentRuntimeCleanupResult:
    """Rows deleted during best-effort terminal agent cleanup."""

    dispatch_mutex_rows: int = 0
    workflow_instance_rows: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


def cleanup_agent_runtime_state(
    db: DatabaseProtocol | None,
    *,
    run_id: str | None,
    child_session_id: str | None,
) -> AgentRuntimeCleanupResult:
    """Release runtime rows tied to a terminal agent run.

    This is best-effort by design: the agent run has already reached a terminal
    state, and stale runtime rows should not keep the caller stuck.
    """

    if db is None:
        return AgentRuntimeCleanupResult()

    dispatch_mutex_rows = 0
    workflow_instance_rows = 0
    errors: list[str] = []

    if run_id:
        try:
            dispatch_mutex_rows = TaskDispatchMutexManager(db).clear_by_run_id(run_id)
        except Exception as exc:
            message = f"dispatch mutex cleanup failed for run {run_id}: {exc}"
            logger.warning(message)
            errors.append(message)

    if child_session_id:
        try:
            workflow_instance_rows = WorkflowInstanceManager(db).delete_instances_for_session(
                child_session_id
            )
        except Exception as exc:
            message = f"workflow instance cleanup failed for session {child_session_id}: {exc}"
            logger.warning(message)
            errors.append(message)

    return AgentRuntimeCleanupResult(
        dispatch_mutex_rows=dispatch_mutex_rows,
        workflow_instance_rows=workflow_instance_rows,
        errors=tuple(errors),
    )
