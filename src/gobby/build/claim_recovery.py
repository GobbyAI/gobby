"""Build-time recovery for safe automation task claims."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from gobby.storage.agents import ACTIVE_AGENT_RUN_STATUSES
from gobby.storage.build_history import best_effort_record_event
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task, TaskArtifactManager

_REVIEW_SAFE_STATES = frozenset({"needs_review", "review_approved"})
_RECOVERABLE_STAGE_STATES = ("ready", "in_progress", "needs_review", "review_approved")
_ARTIFACT_ISOLATION = frozenset({"worktree", "clone"})


WorkspaceFamily = Literal["worktree", "clone", "none"]


@dataclass(frozen=True)
class ClaimRecoverySummary:
    """Outcome counts for a build claim-recovery pass."""

    considered: int = 0
    released: int = 0
    refused: int = 0


@dataclass(frozen=True)
class _ClaimCandidate:
    task: Task
    stage_name: str
    stage_state: str


@dataclass(frozen=True)
class _WorkspaceCheck:
    family: WorkspaceFamily
    path: str | None = None
    dirty_files: tuple[str, ...] = ()
    error: str | None = None


def recover_safe_build_claims(
    db: HubDatabase,
    project_id: str | None,
) -> ClaimRecoverySummary:
    """Release review-safe build claims whose isolation workspace is clean."""
    task_manager = LocalTaskManager(db)
    considered = 0
    released = 0
    refused = 0

    for candidate in _claimed_automation_candidates(db, project_id=project_id):
        considered += 1
        claim = candidate.task.claimed_by_session_id
        if not claim:
            continue

        refusal = _refusal_payload(db, task_manager, candidate, claim)
        if refusal is not None:
            refused += 1
            _record_claim_recovery_event(
                db,
                candidate,
                outcome="refused",
                claim=claim,
                payload=refusal,
            )
            continue

        task_manager.release_task_claim(candidate.task.id)
        released += 1
        _record_claim_recovery_event(
            db,
            candidate,
            outcome="released",
            claim=claim,
            payload={"reason": "review_safe_workspace_clean"},
        )

    return ClaimRecoverySummary(considered=considered, released=released, refused=refused)


def _claimed_automation_candidates(
    db: HubDatabase,
    *,
    project_id: str | None,
) -> list[_ClaimCandidate]:
    params: list[Any] = [*_RECOVERABLE_STAGE_STATES]
    project_filter = ""
    if project_id is not None:
        project_filter = "AND tasks.project_id = ?"
        params.append(project_id)
    state_placeholders = ", ".join("?" for _ in _RECOVERABLE_STAGE_STATES)

    rows = db.fetchall(
        f"""
        SELECT tasks.*,
               current_stage.stage_name AS current_stage_name,
               current_stage.state AS current_stage_state
          FROM tasks
          JOIN task_stage_states current_stage
            ON current_stage.task_id = tasks.id
           AND current_stage.state != 'done'
           AND current_stage.position = (
               SELECT MIN(stage_scan.position)
                 FROM task_stage_states stage_scan
                WHERE stage_scan.task_id = tasks.id
                  AND stage_scan.state != 'done'
           )
         WHERE tasks.allow_automation = 1
           AND tasks.claimed_by_session_id IS NOT NULL
           AND tasks.closed_at IS NULL
           AND tasks.escalated_at IS NULL
           AND COALESCE(tasks.is_escalated, 0) = 0
           AND current_stage.state IN ({state_placeholders})
           {project_filter}
         ORDER BY tasks.priority ASC, tasks.seq_num ASC, tasks.created_at ASC
        """,  # nosec B608 # project_filter and placeholders are assembled from static clauses.
        tuple(params),
    )
    return [
        _ClaimCandidate(
            task=Task.from_row(row),
            stage_name=str(row["current_stage_name"]),
            stage_state=str(row["current_stage_state"]),
        )
        for row in rows
    ]


def _refusal_payload(
    db: HubDatabase,
    task_manager: LocalTaskManager,
    candidate: _ClaimCandidate,
    claim: str,
) -> dict[str, Any] | None:
    if candidate.stage_state not in _REVIEW_SAFE_STATES:
        return {"reason": "unsafe_stage"}

    agent_claim = _agent_claim_payload(db, candidate.task.id, claim)
    if agent_claim is not None:
        return agent_claim

    workspace = _workspace_check(task_manager, candidate.task)
    if workspace.error is not None:
        return {"reason": "workspace_inspection_failed", "workspace": _workspace_payload(workspace)}
    if workspace.dirty_files:
        return {"reason": "dirty_workspace", "workspace": _workspace_payload(workspace)}
    return None


def _agent_claim_payload(
    db: HubDatabase,
    task_id: str,
    claim: str,
) -> dict[str, Any] | None:
    rows = db.fetchall(
        """
        SELECT id, status
          FROM agent_runs
         WHERE task_id = ?
           AND (
               child_session_id = ?
               OR claimed_session_id = ?
               OR parent_session_id = ?
           )
         ORDER BY updated_at DESC
        """,
        (task_id, claim, claim, claim),
    )
    if not rows:
        return None

    for row in rows:
        if row["status"] in ACTIVE_AGENT_RUN_STATUSES:
            return {
                "reason": "active_agent_owned",
                "agent_run_id": row["id"],
                "agent_status": row["status"],
            }

    latest = rows[0]
    return {
        "reason": "agent_claim_recovery_delegated",
        "agent_run_id": latest["id"],
        "agent_status": latest["status"],
    }


def _workspace_check(task_manager: LocalTaskManager, task: Task) -> _WorkspaceCheck:
    artifacts = TaskArtifactManager(task_manager.db).get_artifacts(task.id)
    if artifacts.worktree_path:
        return _inspect_workspace("worktree", artifacts.worktree_path)
    if artifacts.clone_path:
        return _inspect_workspace("clone", artifacts.clone_path)

    isolation = getattr(task.isolation, "value", task.isolation)
    if isolation in _ARTIFACT_ISOLATION:
        return _WorkspaceCheck(
            family="none",
            error=f"missing_{isolation}_artifact_path",
        )
    return _WorkspaceCheck(family="none")


def _inspect_workspace(family: WorkspaceFamily, raw_path: str) -> _WorkspaceCheck:
    path = Path(raw_path)
    if not path.exists():
        return _WorkspaceCheck(family=family, path=raw_path, error="artifact_path_missing")

    dirty_files, error = _git_status_lines(path)
    return _WorkspaceCheck(
        family=family,
        path=str(path),
        dirty_files=tuple(dirty_files),
        error=error,
    )


def _git_status_lines(path: Path) -> tuple[list[str], str | None]:
    try:
        result = subprocess.run(  # nosec B603 # git args are fixed by this helper.
            ["git", "status", "--porcelain"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [], "git_status_timeout"
    except OSError as exc:
        return [], f"git_status_error:{exc}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return [], f"git_status_failed:{detail[:500]}"
    return [line for line in result.stdout.splitlines() if line.strip()], None


def _record_claim_recovery_event(
    db: HubDatabase,
    candidate: _ClaimCandidate,
    *,
    outcome: Literal["released", "refused"],
    claim: str,
    payload: dict[str, Any],
) -> None:
    reason = payload.get("reason")
    event_payload = {
        "outcome": outcome,
        "reason": reason,
        "task_ref": _task_ref(candidate.task),
        "claimed_by_session_id": claim,
        "stage_name": candidate.stage_name,
        "stage_state": candidate.stage_state,
        **payload,
    }
    message = (
        "released safe build automation claim"
        if outcome == "released"
        else f"refused build automation claim release: {reason}"
    )
    best_effort_record_event(
        db,
        project_id=candidate.task.project_id,
        task_id=candidate.task.id,
        event_type="build_claim_recovery",
        action="build_claim_recovery",
        message=message,
        payload=event_payload,
    )


def _workspace_payload(workspace: _WorkspaceCheck) -> dict[str, Any]:
    return {
        "family": workspace.family,
        "path": workspace.path,
        "dirty_files": list(workspace.dirty_files),
        "error": workspace.error,
    }


def _task_ref(task: Task) -> str:
    return f"#{task.seq_num}" if task.seq_num else task.id


__all__ = ["ClaimRecoverySummary", "recover_safe_build_claims"]
