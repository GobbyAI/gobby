"""Stage manifest routes for task HTTP endpoints."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from gobby.storage.tasks._models import TaskNotFoundError
from gobby.storage.tasks._stage_types import (
    IllegalManifestMutationError,
    IllegalStageTransitionError,
    StageManifestSpec,
    StageState,
)

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer
    from gobby.storage.tasks._models import Task


class StagePatchRequest(BaseModel):
    """Request body for mutating a task stage row."""

    action: Literal[
        "start",
        "submit_for_review",
        "approve_review",
        "reject_review",
        "complete",
        "fail",
        "add",
        "remove",
    ]
    notes: str | None = None
    reason: str | None = None
    needs_human: bool = False
    commit_sha: str | None = None
    artifact_updates: dict[str, str] | None = None
    validation_override_reason: str | None = None
    position: int | None = None


class StageStateView(BaseModel):
    """HTTP projection for a task stage row."""

    task_id: str
    stage_name: str
    position: int
    state: Literal["ready", "in_progress", "needs_review", "review_approved", "done"]
    review_policy: Literal["none", "required", "optional"]
    reviewer_agent: str | None
    entered_at: str | None
    entered_by_session_id: str | None
    completed_at: str | None
    completed_by_session_id: str | None
    completed_commit_sha: str | None
    work_attempt_count: int
    review_round_count: int
    max_work_attempts: int | None
    max_review_rounds: int | None
    artifact_refs: dict[str, str] | None
    notes: str | None
    updated_at: str


class TaskStagesResponse(BaseModel):
    task_id: str
    stages: list[StageStateView]


class TaskStageResponse(BaseModel):
    task_id: str
    stage: StageStateView | None = None
    stages: list[StageStateView] | None = None


def _transition_error_payload(error: IllegalStageTransitionError) -> dict[str, Any]:
    return {
        "error": "illegal_stage_transition",
        "stage_name": error.stage_name,
        "current_state": error.current_state,
        "attempted_transition": error.attempted_transition,
        "review_policy": error.review_policy,
    }


def _mutation_error_payload(error: IllegalManifestMutationError) -> dict[str, Any]:
    return {
        "error": "illegal_manifest_mutation",
        "task_id": error.task_id,
        "target_stage_name": error.target_stage_name,
        "target_position": error.target_position,
        "current_stage_name": error.current_stage_name,
        "current_stage_state": error.current_stage_state,
        "mutation": error.mutation,
        "reason": error.reason,
    }


def register_task_stage_routes(
    router: APIRouter,
    server: HTTPServer,
    *,
    resolve_task: Callable[..., Task],
    broadcast_task: Callable[[str, dict[str, Any]], Awaitable[None]],
    stage_view: Callable[[StageState], dict[str, Any]],
) -> None:
    """Register stage manifest routes on the shared tasks router."""

    @router.get("/{task_id}/stages", response_model=TaskStagesResponse)
    async def get_task_stages(task_id: str) -> dict[str, Any]:
        """Get the denormalized stage manifest for a task."""
        try:
            task = resolve_task(task_id)
            return {
                "task_id": task.id,
                "stages": [
                    stage_view(row)
                    for row in server.task_manager.stage_states.list_for_task(task.id)
                ],
            }
        except (ValueError, TaskNotFoundError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @router.patch("/{task_id}/stages/{stage_name}", response_model=TaskStageResponse)
    async def patch_task_stage(
        task_id: str,
        stage_name: str,
        request_data: StagePatchRequest,
    ) -> Any:
        """Apply a stage transition or structural manifest mutation."""
        try:
            task = resolve_task(task_id)
            manager = server.task_manager.stage_states
            if request_data.action == "start":
                stage_row = manager.start_stage(
                    task.id, stage_name, by_session_id=None, notes=request_data.notes
                )
                event = "stage_changed"
            elif request_data.action == "submit_for_review":
                stage_row = manager.submit_for_review(
                    task.id, stage_name, by_session_id=None, notes=request_data.notes
                )
                event = "stage_changed"
            elif request_data.action == "approve_review":
                stage_row = manager.approve_review(
                    task.id, stage_name, by_session_id=None, notes=request_data.notes
                )
                event = "stage_changed"
            elif request_data.action == "reject_review":
                if request_data.reason is None:
                    raise ValueError("reason is required for reject_review")
                stage_row = manager.reject_review(
                    task.id,
                    stage_name,
                    reason=request_data.reason,
                    by_session_id=None,
                    notes=request_data.notes,
                )
                event = "stage_changed"
            elif request_data.action == "complete":
                stage_row = manager.complete_stage(
                    task.id,
                    stage_name,
                    by_session_id=None,
                    commit_sha=request_data.commit_sha,
                    artifact_updates=request_data.artifact_updates,
                    validation_override_reason=request_data.validation_override_reason,
                )
                event = "stage_changed"
            elif request_data.action == "fail":
                if request_data.reason is None:
                    raise ValueError("reason is required for fail")
                stage_row = manager.fail_stage(
                    task.id,
                    stage_name,
                    reason=request_data.reason,
                    needs_human=request_data.needs_human,
                    by_session_id=None,
                )
                event = "stage_changed"
            elif request_data.action == "add":
                if request_data.position is None:
                    raise ValueError("position is required for add")
                stage_row = manager.add_stage(
                    task.id,
                    StageManifestSpec(stage_name=stage_name, position=request_data.position),
                    by_session_id=None,
                )
                event = "stage_manifest_changed"
            else:
                manager.remove_stage(task.id, stage_name, by_session_id=None)
                stage_row = None
                event = "stage_manifest_changed"

            response = {
                "task_id": task.id,
                "stage": stage_view(stage_row) if stage_row else None,
                "stages": [
                    stage_view(row)
                    for row in server.task_manager.stage_states.list_for_task(task.id)
                ],
            }
            await broadcast_task(
                event,
                {
                    "id": task.id,
                    "stage_name": stage_name,
                    "state": stage_row.state if stage_row else None,
                    "stages": response["stages"],
                },
            )
            return response
        except IllegalStageTransitionError as e:
            return JSONResponse(status_code=422, content=_transition_error_payload(e))
        except IllegalManifestMutationError as e:
            return JSONResponse(status_code=422, content=_mutation_error_payload(e))
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
