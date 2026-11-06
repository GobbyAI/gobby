"""Cleanup helpers for terminal agent runtime state."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from gobby.storage.clones import LocalCloneManager
from gobby.storage.daemon_resume_keys import DISPATCH_LEASE_HELD_KEY
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.workflows.step_instances import AgentStepInstanceManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentRuntimeCleanupResult:
    """Rows deleted during best-effort terminal agent cleanup."""

    dispatch_mutex_rows: int = 0
    workflow_instance_rows: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


def _record_spawn_lease_for_resume(db: HubDatabase, run_id: str) -> None:
    """Record on a parked run whether its own spawn took its task lease, so resume re-takes it.

    Always writes the boolean. Resume copies the predecessor's metadata into the successor,
    so a stale True from an earlier park must be overwritten rather than left to leak forward.
    """
    from gobby.storage.agents import LocalAgentRunManager

    mutex = TaskDispatchMutexManager(db).get_mutex_by_run_id(run_id)
    held = mutex is not None and mutex.action_kind == "spawn_agent"
    LocalAgentRunManager(db).merge_resume_metadata(run_id, {DISPATCH_LEASE_HELD_KEY: held})


def cleanup_agent_runtime_state(
    db: HubDatabase | None,
    *,
    run_id: str | None,
    child_session_id: str | None,
    terminal_reason: str | None,
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
    preserve_session_state = terminal_reason == "daemon_stop"
    if run_id and terminal_reason == "spawn_rollback":
        from gobby.storage.agents import LocalAgentRunManager

        try:
            run = LocalAgentRunManager(db).get(run_id)
            metadata = run.resume_metadata_json if run else None
            preserve_session_state = bool(metadata and metadata.get("resume_existing_session"))
        except Exception as exc:
            preserve_session_state = True
            message = f"resume state lookup failed for run {run_id}: {exc}"
            logger.warning(message)
            errors.append(message)

    if run_id:
        if terminal_reason == "daemon_stop":
            try:
                _record_spawn_lease_for_resume(db, run_id)
            except Exception as exc:
                message = f"dispatch lease snapshot failed for run {run_id}: {exc}"
                logger.warning(message)
                errors.append(message)
        try:
            dispatch_mutex_rows = TaskDispatchMutexManager(db).clear_by_run_id(run_id)
        except Exception as exc:
            message = f"dispatch mutex cleanup failed for run {run_id}: {exc}"
            logger.warning(message)
            errors.append(message)

    if child_session_id and not preserve_session_state:
        try:
            workflow_instance_rows = AgentStepInstanceManager(db).delete_for_session(
                child_session_id
            )
        except Exception as exc:
            message = f"agent-step instance cleanup failed for session {child_session_id}: {exc}"
            logger.warning(message)
            errors.append(message)

    return AgentRuntimeCleanupResult(
        dispatch_mutex_rows=dispatch_mutex_rows,
        workflow_instance_rows=workflow_instance_rows,
        errors=tuple(errors),
    )


def release_session_end_run(
    db: HubDatabase,
    run_id: str,
    clone_id: str | None,
    child_session_id: str,
) -> AgentRuntimeCleanupResult:
    """Hand back the clone and dispatch mutex of a run its session end terminalized.

    Same order as ``TerminalResourceCleaner.post_terminal_cleanup``: clone ownership
    before the mutex, so a same-task respawn finds both free.
    """
    if clone_id:
        try:
            LocalCloneManager(db).release(clone_id)
        except Exception as exc:
            logger.warning("Failed to release clone %s for agent run %s: %s", clone_id, run_id, exc)
    return cleanup_agent_runtime_state(
        db,
        run_id=run_id,
        child_session_id=child_session_id,
        terminal_reason=None,
    )
