"""Dispatcher recovery for daemon-stop interrupted agent runs."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from gobby.agents.resume_executor import resume_agent_run
from gobby.dispatch.actions import SpawnAgentAction
from gobby.dispatch.context import _field
from gobby.dispatch.mutex import RuntimeDispatchMutex
from gobby.storage.agent_resume import increment_daemon_resume_failure_count
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import TaskAlreadyEscalatedError
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._read import get_task
from gobby.storage.tasks._transitions import escalate_task
from gobby.storage.tasks._updates import update_task
from gobby.utils.git import git_subprocess_env

logger = logging.getLogger(__name__)

_AUDIT_HEADING = "Agent resume after daemon restart failed"
_ESCALATION_REASON = "agent_resume_after_daemon_restart_failed"
_MAX_RESUME_FAILURES = 3
_MAX_RESUME_RETRY_DELAY_SECONDS = 30.0
_WORKSPACE_DIRTY_CHECK_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class DaemonStopResumeResult:
    attempted: bool
    handled: bool
    run_id: str | None = None


async def try_resume_daemon_stop_run(
    action: SpawnAgentAction,
    *,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    context: object | None,
    services: object | None,
) -> DaemonStopResumeResult:
    """Attempt daemon-stop resume before dispatcher performs a fresh spawn."""
    candidate = _find_resume_candidate(action, db=db, context=context)
    if candidate is None:
        return DaemonStopResumeResult(attempted=False, handled=False)

    metadata = candidate.resume_metadata_json or {}
    workspace_path = _workspace_path(metadata)
    workspace_dirty = await _workspace_dirty(workspace_path)
    runner = getattr(services, "agent_runner", None)
    session_manager = getattr(services, "session_manager", None)
    if runner is None or session_manager is None:
        error = "services_missing:agent_runner,session_manager"
        return _handle_resume_failure(
            action,
            mutex,
            db,
            candidate,
            workspace_path,
            workspace_dirty,
            error,
            failure_count=increment_daemon_resume_failure_count(db, run_id=candidate.id),
        )

    resume_result = await resume_agent_run(
        candidate,
        resume_metadata=metadata,
        runner=runner,
        session_manager=session_manager,
        daemon_config=getattr(services, "config", None),
        completion_registry=getattr(services, "completion_registry", None),
    )
    if resume_result.success and resume_result.run_id:
        mutex.attach(str(resume_result.run_id))
        return DaemonStopResumeResult(
            attempted=True,
            handled=True,
            run_id=str(resume_result.run_id),
        )

    # The retry budget counts failures only; a successful resume consumes the
    # original, and successors start each recovery episode at zero.
    return _handle_resume_failure(
        action,
        mutex,
        db,
        candidate,
        workspace_path,
        workspace_dirty,
        resume_result.error or "resume_failed",
        failure_count=increment_daemon_resume_failure_count(db, run_id=candidate.id),
    )


def _find_resume_candidate(
    action: SpawnAgentAction,
    *,
    db: HubDatabase,
    context: object | None,
) -> AgentRun | None:
    stage_name, stage_state = _action_stage(action, context)
    for run in LocalAgentRunManager(db).list_daemon_stop_resume_candidates(action.task_id):
        metadata = run.resume_metadata_json or {}
        if not metadata:
            continue
        if not _matches_agent(action, run, metadata):
            continue
        if not _matches_stage(metadata, stage_name, stage_state):
            continue
        return run
    return None


def _handle_resume_failure(
    action: SpawnAgentAction,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    candidate: AgentRun,
    workspace_path: str | None,
    workspace_dirty: bool,
    error: str,
    *,
    failure_count: int,
) -> DaemonStopResumeResult:
    if not workspace_dirty and failure_count < _MAX_RESUME_FAILURES:
        logger.info(
            "Daemon-stop resume remains recovery-pending after clean failure",
            extra={
                "task_id": action.task_id,
                "run_id": candidate.id,
                "error": error,
                "failure_count": failure_count,
            },
        )
        TaskDispatchMutexManager(db).clear_by_run_id(candidate.id)
        mutex.release()
        from gobby.build.dispatch_tick import schedule_dispatcher_tick_for_task

        delay_seconds = min(
            2 ** max(failure_count - 1, 0),
            _MAX_RESUME_RETRY_DELAY_SECONDS,
        )
        asyncio.get_running_loop().call_later(
            delay_seconds,
            partial(
                schedule_dispatcher_tick_for_task,
                db,
                task_id=action.task_id,
                reason="daemon_resume_retry",
            ),
        )
        return DaemonStopResumeResult(attempted=True, handled=True)

    _append_resume_failure_marker(
        db,
        action.task_id,
        candidate,
        workspace_path=workspace_path,
        workspace_dirty=workspace_dirty,
        error=error,
    )
    try:
        reason = (
            _ESCALATION_REASON
            if workspace_dirty
            else f"daemon_stop_resume_retry_exhausted:{failure_count}"
        )
        try:
            escalate_task(db, action.task_id, reason=reason)
        except (TaskAlreadyEscalatedError, ValueError):
            logger.warning(
                "Failed to escalate task after daemon-stop resume failure",
                extra={"task_id": action.task_id, "run_id": candidate.id},
                exc_info=True,
            )
    finally:
        TaskDispatchMutexManager(db).clear_by_run_id(candidate.id)
        mutex.release()
    return DaemonStopResumeResult(attempted=True, handled=True)


def _append_resume_failure_marker(
    db: HubDatabase,
    task_id: str,
    run: AgentRun,
    *,
    workspace_path: str | None,
    workspace_dirty: bool,
    error: str,
) -> bool:
    task = get_task(db, task_id)
    description = task.description or ""
    closing = (
        "The workspace was dirty, so Gobby preserved it and did not start a fresh agent."
        if workspace_dirty
        else (
            "Same-session resume retries were exhausted; Gobby preserved the "
            "durable session and did not start a fresh agent."
        )
    )
    body = (
        f"Original run: {run.id}\n\n"
        f"Workspace: {workspace_path or 'unknown'}\n\n"
        f"Error: {error}\n\n"
        f"{closing}"
    )
    update_task(db, task_id, description=f"{description}\n\n### {_AUDIT_HEADING}\n\n{body}")
    return True


async def _workspace_dirty(workspace_path: str | None) -> bool:
    if not workspace_path:
        return False
    path = Path(workspace_path).expanduser()
    try:
        path = path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        logger.debug("Failed to canonicalize workspace path %s", workspace_path, exc_info=True)
        return True
    if not path.is_dir():
        return False
    git_env = git_subprocess_env()
    git_executable = shutil.which("git", path=git_env.get("PATH") if git_env else None)
    if git_executable is None:
        logger.debug("Cannot inspect workspace dirty state for %s: git executable not found", path)
        return True
    try:
        repo_result = await asyncio.to_thread(
            subprocess.run,
            [git_executable, "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=_WORKSPACE_DIRTY_CHECK_TIMEOUT_SECONDS,
            check=False,
            env=git_env,
        )
    except subprocess.TimeoutExpired:
        logger.debug("Timed out probing workspace repository state for %s", path, exc_info=True)
        return True
    except (OSError, subprocess.SubprocessError):
        logger.debug("Failed to probe workspace repository state for %s", path, exc_info=True)
        return True
    if repo_result.returncode != 0 or repo_result.stdout.strip().lower() != "true":
        logger.debug(
            "Workspace path %s is not an inspectable git work tree: return code %s stderr=%r",
            path,
            repo_result.returncode,
            repo_result.stderr,
        )
        return False
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [git_executable, "-C", str(path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=_WORKSPACE_DIRTY_CHECK_TIMEOUT_SECONDS,
            check=False,
            env=git_env,
        )
    except subprocess.TimeoutExpired:
        logger.debug("Timed out inspecting workspace dirty state for %s", path, exc_info=True)
        return True
    except (OSError, subprocess.SubprocessError):
        logger.debug("Failed to inspect workspace dirty state for %s", path, exc_info=True)
        return True
    if result.returncode != 0:
        logger.debug(
            "git status --porcelain failed for %s with return code %s stdout=%r stderr=%r",
            path,
            result.returncode,
            result.stdout,
            result.stderr,
        )
        return True
    return bool(result.stdout.strip())


def _workspace_path(metadata: dict[str, Any]) -> str | None:
    for key in ("cwd", "workspace_path", "worktree_path", "clone_path"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _matches_agent(
    action: SpawnAgentAction,
    run: AgentRun,
    metadata: dict[str, Any],
) -> bool:
    agent_slug = metadata.get("agent_slug") or run.agent_name
    return agent_slug == action.agent_slug


def _matches_stage(
    metadata: dict[str, Any],
    stage_name: str | None,
    stage_state: str | None,
) -> bool:
    metadata_stage_name = metadata.get("stage_name")
    metadata_stage_state = metadata.get("stage_state")
    if stage_name and isinstance(metadata_stage_name, str) and metadata_stage_name != stage_name:
        return False
    if (
        stage_state
        and isinstance(metadata_stage_state, str)
        and metadata_stage_state != stage_state
    ):
        return False
    return True


def _action_stage(
    action: SpawnAgentAction,
    context: object | None,
) -> tuple[str | None, str | None]:
    initial = action.initial_variables or {}
    stage_name = initial.get("stage_name")
    stage_state = initial.get("stage_state")
    if isinstance(stage_name, str) and isinstance(stage_state, str):
        return stage_name, stage_state
    stage = _field(context, "current_stage")
    context_stage_name = _field(stage, "stage_name", _field(stage, "name"))
    context_stage_state = _field(stage, "state")
    return (
        context_stage_name if isinstance(context_stage_name, str) else None,
        context_stage_state if isinstance(context_stage_state, str) else None,
    )
