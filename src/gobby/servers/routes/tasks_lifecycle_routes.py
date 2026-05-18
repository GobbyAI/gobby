"""Lifecycle and ownership routes for task HTTP endpoints."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from gobby.servers.routes.tasks_assignment import TaskAssignmentNotifier
from gobby.storage.tasks._models import TaskNotFoundError

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer
    from gobby.storage.tasks._models import Task

logger = logging.getLogger(__name__)


class TaskClaimRequest(BaseModel):
    """Request body for claiming a task."""

    session_id: str = Field(..., description="Owning session reference or UUID")
    force: bool = Field(default=False, description="Override an existing claim")


class TaskReleaseClaimRequest(BaseModel):
    """Request body for releasing task ownership."""

    model_config = ConfigDict(extra="forbid")


class TaskEscalateRequest(BaseModel):
    """Request body for escalation."""

    reason: str = Field(..., description="Why this task needs escalation")


class TaskCloseRequest(BaseModel):
    """Request body for closing a task."""

    reason: str | None = Field(default=None, description="Reason for closing")
    commit_sha: str | None = Field(default=None, description="Git commit SHA to link")
    session_id: str | None = Field(
        default=None,
        description="Session reference or UUID that closed the task",
    )


class TaskReopenRequest(BaseModel):
    """Request body for reopening a task."""

    reason: str | None = Field(default=None, description="Reason for reopening")


class TaskDeEscalateRequest(BaseModel):
    """Request body for de-escalating a task."""

    model_config = ConfigDict(extra="forbid")

    decision_context: str = Field(..., description="User's decision or instructions for the agent")
    reset_validation: bool = Field(default=False, description="Also reset validation fail count")


ResolveTask = Callable[..., "Task"]
ResolveSessionRef = Callable[..., str]
BroadcastTask = Callable[[str, dict[str, Any]], Awaitable[None]]


def _warning_from_exception(source: str, exc: BaseException) -> dict[str, str]:
    error = str(exc).strip() or type(exc).__name__
    return {"source": source, "error": error}


def register_task_lifecycle_routes(
    router: APIRouter,
    server: HTTPServer,
    *,
    resolve_task: ResolveTask,
    resolve_session_ref: ResolveSessionRef,
    broadcast_task: BroadcastTask,
    broadcast_claim_task: BroadcastTask | None = None,
) -> None:
    """Register task lifecycle and ownership routes on the shared tasks router."""
    assignment_notifier = TaskAssignmentNotifier(server)
    claim_broadcaster = broadcast_claim_task or broadcast_task

    @router.post("/{task_id}/claim")
    async def claim_task(task_id: str, request_data: TaskClaimRequest) -> Any:
        """Claim a task for a session."""
        try:
            task = resolve_task(task_id)
            resolved_id = task.id
            resolved_session_id = resolve_session_ref(
                request_data.session_id,
                project_id=task.project_id,
            )
            claimed_task = server.task_manager.claim_task(
                resolved_id,
                session_id=resolved_session_id,
                force=request_data.force,
            )
            result = claimed_task.to_dict()
            warnings: list[dict[str, str]] = []
            try:
                await assignment_notifier.send(
                    task_dict=result,
                    to_session_id=resolved_session_id,
                )
            except Exception as exc:
                logger.exception(
                    "Task claim committed but assignment notification failed",
                    extra={
                        "task_id": resolved_id,
                        "task_ref": result.get("ref"),
                        "to_session_id": resolved_session_id,
                    },
                )
                warnings.append(_warning_from_exception("assignment_notification", exc))
            try:
                await claim_broadcaster("task_claimed", result)
            except Exception as exc:
                logger.exception(
                    "Task claim committed but broadcast failed",
                    extra={"task_id": resolved_id, "task_ref": result.get("ref")},
                )
                warnings.append(_warning_from_exception("broadcast", exc))
            if warnings:
                result["warnings"] = warnings
            return result
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/{task_id}/release-claim")
    async def release_task_claim(
        task_id: str, request_data: TaskReleaseClaimRequest | None = None
    ) -> Any:
        """Release canonical task ownership without using generic PATCH."""
        _ = request_data
        try:
            task = resolve_task(task_id)
            resolved_id = task.id
            released = server.task_manager.release_task_claim(resolved_id)
            result = released.to_dict()
            await broadcast_task("task_claim_released", result)
            return result
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/{task_id}/escalate")
    async def escalate_task(task_id: str, request_data: TaskEscalateRequest) -> Any:
        """Escalate a task without using generic mutation."""
        try:
            task = resolve_task(task_id)
            resolved_id = task.id
            updated = server.task_manager.escalate_task(resolved_id, reason=request_data.reason)
            result = updated.to_dict()
            await broadcast_task("task_escalated", result)
            return result
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/{task_id}/close")
    async def close_task(task_id: str, request_data: TaskCloseRequest | None = None) -> Any:
        """Close a task."""
        try:
            task = resolve_task(task_id)
            resolved_id = task.id
            body = request_data or TaskCloseRequest()
            resolved_session_id = (
                resolve_session_ref(body.session_id, project_id=task.project_id)
                if body.session_id is not None
                else None
            )

            if body.commit_sha:
                server.task_manager.link_commit(resolved_id, body.commit_sha)

            closed = server.task_manager.close_task(
                resolved_id,
                reason=body.reason,
                closed_in_session_id=resolved_session_id,
                closed_commit_sha=body.commit_sha,
            )
            result = closed.to_dict()
            await broadcast_task("task_closed", result)
            return result
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/{task_id}/reopen")
    async def reopen_task(task_id: str, request_data: TaskReopenRequest | None = None) -> Any:
        """Reopen a closed task."""
        try:
            task = resolve_task(task_id)
            resolved_id = task.id
            body = request_data or TaskReopenRequest()
            reopened = server.task_manager.reopen_task(resolved_id, reason=body.reason)
            result = reopened.to_dict()
            await broadcast_task("task_reopened", result)
            return result
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/{task_id}/de-escalate")
    async def de_escalate_task(task_id: str, request_data: TaskDeEscalateRequest) -> Any:
        """De-escalate a task with user decision context."""
        try:
            task = resolve_task(task_id)
            resolved_id = task.id
            updated = server.task_manager.de_escalate_task(
                resolved_id,
                reason=request_data.decision_context,
                reset_validation=request_data.reset_validation,
            )
            result = updated.to_dict()
            await broadcast_task("task_de_escalated", result)
            return result
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
