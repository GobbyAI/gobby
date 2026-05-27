"""Recovery checks for build integration workspace artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gobby.build.workspace_common import BuildWorkspaceError, WorkspaceBackend
from gobby.storage.clones import Clone
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import Worktree


@dataclass(frozen=True)
class ActiveWorkspaceRun:
    """Active agent run that still references an integration workspace id."""

    run_id: str
    status: str
    agent_name: str | None
    task_id: str | None
    child_session_id: str | None


def recover_stale_integration_artifact(
    *,
    db: HubDatabase,
    task_manager: LocalTaskManager,
    task_id: str,
    backend: WorkspaceBackend,
    workspace_id: str,
    record: Worktree | Clone | None,
) -> bool:
    """Clear a stale integration artifact id when no active run owns it."""
    if record is not None:
        path = _record_path(record)
        if path is not None and path.is_dir():
            return False

    active_run = _active_workspace_run(db, backend, workspace_id)
    if active_run is not None:
        raise BuildWorkspaceError(_active_workspace_message(backend, workspace_id, active_run))

    if backend == "worktree":
        task_manager.artifacts.set_artifacts_atomic(task_id, integration_workspace_id=None)
    else:
        task_manager.artifacts.set_artifacts_atomic(task_id, integration_clone_id=None)
    return True


def _is_promotable_workspace(
    record: Worktree | Clone | None,
    task_id: str,
    backend: WorkspaceBackend,
) -> bool:
    if not _is_recoverable_workspace(record, task_id, backend):
        return False
    return getattr(record, "workspace_role", "task") == "task"


def _is_recoverable_workspace(
    record: Worktree | Clone | None,
    task_id: str,
    backend: WorkspaceBackend,
) -> bool:
    if record is None or record.task_id != task_id:
        return False
    if getattr(record, "workspace_role", "task") not in {"task", "integration"}:
        return False
    path = _record_path(record)
    if path is None or not path.is_dir():
        return False
    if not getattr(record, "base_branch", None):
        raise BuildWorkspaceError(f"{backend} base branch is required for integration promotion")
    return True


def _record_path(record: Worktree | Clone) -> Path | None:
    raw_path = getattr(record, "worktree_path", None) or getattr(record, "clone_path", None)
    if not raw_path:
        return None
    return Path(str(raw_path))


def _active_workspace_run(
    db: HubDatabase,
    backend: WorkspaceBackend,
    workspace_id: str,
) -> ActiveWorkspaceRun | None:
    column = "worktree_id" if backend == "worktree" else "clone_id"
    row = db.fetchone(
        f"""
        SELECT id, status, agent_name, task_id, child_session_id
          FROM agent_runs
         WHERE {column} = ?
           AND status IN ('pending', 'running')
         ORDER BY created_at DESC
         LIMIT 1
        """,  # nosec B608 # column is selected from static backend values.
        (workspace_id,),
    )
    if row is None:
        return None
    return ActiveWorkspaceRun(
        run_id=str(row["id"]),
        status=str(row["status"]),
        agent_name=_optional_str(row["agent_name"]),
        task_id=_optional_str(row["task_id"]),
        child_session_id=_optional_str(row["child_session_id"]),
    )


def _active_workspace_message(
    backend: WorkspaceBackend,
    workspace_id: str,
    run: ActiveWorkspaceRun,
) -> str:
    agent = run.agent_name or "unknown-agent"
    session = f", session {run.child_session_id}" if run.child_session_id else ""
    return (
        f"integration {backend} {workspace_id} is still referenced by active run "
        f"{run.run_id} ({agent}, status={run.status}{session}); stop or restart the build "
        "before recovering integration workspace metadata"
    )


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None
