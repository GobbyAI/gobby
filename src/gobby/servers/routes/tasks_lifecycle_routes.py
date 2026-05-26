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


class TaskEscalateRequest(BaseModel):
    """Request body for escalation."""

    reason: str = Field(..., description="Why this task needs escalation")


class TaskCloseRequest(BaseModel):
    """Request body for closing a task."""

    reason: str | None = Field(default=None, description="Reason for closing")
    commit_sha: str | None = Field(default=None, description="Git commit SHA to link")
    force: bool = Field(default=False, description="Close even when child tasks remain open")
    validation_override_reason: str | None = Field(
        default=None,
        description="Why validation was manually overridden",
    )
    session_id: str | None = Field(
        default=None,
        description="Session reference or UUID that closed the task",
    )


class TaskReopenRequest(BaseModel):
    """Request body for reopening a task."""

    reason: str | None = Field(default=None, description="Reason for reopening")


class TaskDeEscalateRequest(BaseModel):
    """Request body for de-escalating a task."""

    # Strict body parsing prevents accidental or hostile lifecycle fields from
    # being smuggled into a privileged de-escalation decision payload.
    model_config = ConfigDict(extra="forbid")

    decision_context: str = Field(..., description="User's decision or instructions for the agent")
    reset_validation: bool = Field(default=False, description="Also reset validation fail count")
    reset_stage_attempts: bool = Field(
        default=False,
        description="Also reset the current stage work attempt count",
    )


ResolveTask = Callable[[str], "Task"]
ResolveSessionRef = Callable[[str, str | None], str]
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

    async def broadcast_with_warning(
        event_type: str,
        task_dict: dict[str, Any],
        broadcaster: BroadcastTask = broadcast_task,
    ) -> list[dict[str, str]]:
        try:
            await broadcaster(event_type, task_dict)
        except Exception as exc:
            logger.exception(
                "Task lifecycle committed but broadcast failed",
                extra={
                    "event_type": event_type,
                    "task_id": task_dict.get("id"),
                    "task_ref": task_dict.get("ref"),
                },
            )
            return [_warning_from_exception("broadcast", exc)]
        return []

    @router.post("/{task_id}/claim")
    async def claim_task(task_id: str, request_data: TaskClaimRequest) -> Any:
        """Claim a task for a session."""
        try:
            task = resolve_task(task_id)
            resolved_id = task.id
            resolved_session_id = resolve_session_ref(
                request_data.session_id,
                task.project_id,
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
            warnings.extend(await broadcast_with_warning("task_claimed", result, claim_broadcaster))
            if warnings:
                result["warnings"] = warnings
            return result
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/{task_id}/release-claim")
    async def release_task_claim(task_id: str) -> Any:
        """Release canonical task ownership without using generic PATCH."""
        try:
            task = resolve_task(task_id)
            resolved_id = task.id
            released = server.task_manager.release_task_claim(resolved_id)
            result = released.to_dict()
            warnings = await broadcast_with_warning("task_claim_released", result)
            if warnings:
                result["warnings"] = warnings
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
            warnings = await broadcast_with_warning("task_escalated", result)
            if warnings:
                result["warnings"] = warnings
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
                resolve_session_ref(body.session_id, task.project_id)
                if body.session_id is not None
                else None
            )

            if body.commit_sha:
                closed = server.task_manager.close_task_with_commit(
                    resolved_id,
                    body.commit_sha,
                    reason=body.reason,
                    force=body.force,
                    closed_in_session_id=resolved_session_id,
                    validation_override_reason=body.validation_override_reason,
                )
            else:
                closed = server.task_manager.close_task(
                    resolved_id,
                    reason=body.reason,
                    force=body.force,
                    closed_in_session_id=resolved_session_id,
                    validation_override_reason=body.validation_override_reason,
                )
            result = closed.to_dict()
            warnings = await broadcast_with_warning("task_closed", result)
            if warnings:
                result["warnings"] = warnings
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
            warnings = await broadcast_with_warning("task_reopened", result)
            if warnings:
                result["warnings"] = warnings
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
                reset_stage_attempts=request_data.reset_stage_attempts,
            )
            result = updated.to_dict()
            warnings = await broadcast_with_warning("task_de_escalated", result)
            if warnings:
                result["warnings"] = warnings
            return result
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
