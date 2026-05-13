"""
Task routes for Gobby HTTP server.

Provides CRUD, list, stage-transition, and dependency endpoints for the task system.
"""

import logging
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gobby.servers.routes.tasks_comment_routes import register_task_comment_routes
from gobby.servers.routes.tasks_dependency_routes import register_task_dependency_routes
from gobby.servers.routes.tasks_stage_routes import register_task_stage_routes
from gobby.storage.tasks._models import (
    TASK_TYPE_CHOICES,
    VALID_CATEGORIES,
    TaskNotFoundError,
    validate_task_type,
)
from gobby.storage.tasks._stage_types import StageState
from gobby.storage.tasks._stage_views import stage_state_view
from gobby.tasks.isolation import validate_task_isolation_artifacts

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer
    from gobby.storage.tasks._models import Task

logger = logging.getLogger(__name__)


# =============================================================================
# Request/Response models
# =============================================================================


class TaskCreateRequest(BaseModel):
    """Request body for creating a task."""

    title: str = Field(..., description="Task title")
    description: str | None = Field(default=None, description="Detailed description")
    priority: int = Field(
        default=2, description="Priority (0=Critical, 1=High, 2=Medium, 3=Low, 4=Backlog)"
    )
    task_type: str = Field(
        default="task",
        description="Task type",
        json_schema_extra={"enum": list(TASK_TYPE_CHOICES)},
    )
    parent_task_id: str | None = Field(default=None, description="Parent task ID")
    labels: list[str] | None = Field(default=None, description="Labels for categorization")
    category: str | None = Field(
        default=None,
        description=f"Task domain: {', '.join(sorted(VALID_CATEGORIES))}",
    )
    validation_criteria: str | None = Field(default=None, description="Acceptance criteria")
    assignee: str | None = Field(default=None, description="Assignee session ID")
    project_id: str | None = Field(
        default=None, description="Project ID (resolved from cwd if omitted)"
    )

    @field_validator("task_type")
    @classmethod
    def _validate_task_type(cls, value: str) -> str:
        return validate_task_type(value)


class TaskUpdateRequest(BaseModel):
    """Request body for updating a task."""

    model_config = ConfigDict(extra="allow")

    title: str | None = Field(default=None, description="New title")
    description: str | None = Field(default=None, description="New description")
    priority: int | None = Field(default=None, description="New priority")
    task_type: str | None = Field(
        default=None,
        description="New task type",
        json_schema_extra={"enum": list(TASK_TYPE_CHOICES)},
    )
    assignee: str | None = Field(
        default=None,
        description="Compatibility field only. Use /claim or /release-claim endpoints instead.",
    )
    labels: list[str] | None = Field(default=None, description="New labels")
    parent_task_id: str | None = Field(default=None, description="New parent task ID")
    category: str | None = Field(default=None, description="New category")
    validation_criteria: str | None = Field(default=None, description="New validation criteria")
    allow_automation: bool | None = Field(
        default=None,
        description="Enable or disable dispatcher automation for this task.",
    )
    isolation: Literal["none", "worktree", "clone"] | None = Field(
        default=None,
        description="Automation isolation mode for future dispatch.",
    )

    @field_validator("task_type")
    @classmethod
    def _validate_task_type(cls, value: str | None) -> str | None:
        return validate_task_type(value) if value is not None else None


class TaskClaimRequest(BaseModel):
    """Request body for claiming a task."""

    session_id: str = Field(..., description="Owning session reference or UUID")
    force: bool = Field(default=False, description="Override an existing claim")


class TaskReleaseClaimRequest(BaseModel):
    """Request body for releasing task ownership."""

    model_config = ConfigDict(extra="forbid")

    pass


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


# =============================================================================
# Router
# =============================================================================


def create_tasks_router(server: "HTTPServer") -> APIRouter:
    """Create tasks router with endpoints bound to server instance."""
    router = APIRouter(prefix="/api/tasks", tags=["tasks"])

    def _resolve_project(project_id: str | None) -> str:
        """Resolve project ID, falling back to server's project context."""
        if project_id:
            return project_id
        return server.resolve_project_id(project_id=None, cwd=None)

    def _resolve_task(task_id: str, *, project_id: str | None = None) -> "Task":
        """Resolve flexible task refs using project context for seq-num lookups."""
        task_id = task_id.strip()
        if not task_id:
            raise ValueError("task_id must be non-empty")
        resolved_project = project_id
        if task_id.startswith("#") or task_id.isdigit():
            resolved_project = _resolve_project(project_id)
        task = server.task_manager.get_task(task_id, project_id=resolved_project)
        if task is None:
            raise TaskNotFoundError(task_id)
        return cast("Task", task)

    async def _broadcast_task(event: str, task_dict: dict[str, Any]) -> None:
        """Broadcast a task event via WebSocket if available."""
        ws = server.services.websocket_server
        if ws:
            try:
                await ws.broadcast_task_event(
                    event, task_id=task_dict.get("id", ""), task=task_dict
                )
            except Exception as e:
                logger.debug(f"Failed to broadcast task event {event}: {e}")

    def _resolve_session_ref(session_ref: str, *, project_id: str | None) -> str:
        """Resolve session references to canonical UUIDs before storage writes."""
        if server.session_manager is None:
            return session_ref
        return str(server.session_manager.resolve_session_reference(session_ref, project_id))

    def _stage_view(stage: StageState) -> dict[str, Any]:
        return stage_state_view(stage)

    def _stage_views_for_tasks(task_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not task_ids:
            return {}
        placeholders = ", ".join("?" for _ in task_ids)
        rows = server.task_manager.db.fetchall(
            f"""
            SELECT *
              FROM task_stage_states
             WHERE task_id IN ({placeholders})
             ORDER BY task_id, position, stage_name
            """,  # nosec B608 # placeholders are generated from task_ids length only.
            tuple(task_ids),
        )
        grouped: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in task_ids}
        for row in rows:
            stage = server.task_manager.stage_states._state_from_row(row)
            grouped.setdefault(stage.task_id, []).append(_stage_view(stage))
        return grouped

    def _normalize_stage_filters(values: list[str] | None) -> list[str]:
        if not values:
            return []
        stage_names: list[str] = []
        seen: set[str] = set()
        for raw_value in values:
            for raw_stage in raw_value.split(","):
                stage_name = raw_stage.strip()
                if not stage_name or stage_name in seen:
                    continue
                stage_names.append(stage_name)
                seen.add(stage_name)
        return stage_names

    register_task_stage_routes(
        router,
        server,
        resolve_task=_resolve_task,
        broadcast_task=_broadcast_task,
        stage_view=_stage_view,
    )

    # -----------------------------------------------------------------
    # List / Stats
    # -----------------------------------------------------------------

    @router.get("")
    async def list_tasks(
        request: Request,
        project_id: str | None = Query(None, description="Filter by project ID"),
        current_stage_state: (
            Literal["ready", "in_progress", "needs_review", "review_approved"] | None
        ) = Query(None, description="Filter by current stage state"),
        claimed: bool | None = Query(None, description="Filter by whether the task is claimed"),
        closed: bool | None = Query(None, description="Filter by canonical closed state"),
        priority: int | None = Query(None, description="Filter by priority"),
        task_type: str | None = Query(None, description="Filter by task type"),
        assignee: str | None = Query(None, description="Filter by assignee"),
        label: str | None = Query(None, description="Filter by label"),
        parent_task_id: str | None = Query(None, description="Filter by parent task ID"),
        search: str | None = Query(None, description="Search by title"),
        limit: int = Query(50, description="Maximum results"),
        offset: int = Query(0, description="Pagination offset"),
        sort_by: str = Query(
            "hierarchy",
            description="Sort order: hierarchy, updated_at, created_at, or priority",
        ),
        sort_order: str = Query("asc", description="Sort direction: asc or desc"),
        stage: list[str] | None = Query(None, description="Filter by stage name"),
        stage_state: (
            Literal["ready", "in_progress", "needs_review", "review_approved", "done"] | None
        ) = Query(None, description="Filter by stage state"),
        include_stages: bool = Query(False, description="Include denormalized stage manifest"),
    ) -> dict[str, Any]:
        """List tasks with optional filters and state distribution stats."""
        try:
            legacy_stage_key = "lifecycle_" + "stage"
            unsupported_filters = {"status", legacy_stage_key} & set(request.query_params)
            if unsupported_filters:
                names = ", ".join(sorted(unsupported_filters))
                raise ValueError(
                    f"Unsupported legacy task filter(s): {names}. Use current_stage_state."
                )
            resolved_project = _resolve_project(project_id)
            stage_task_ids: set[str] | None = None
            stage_filters = _normalize_stage_filters(stage)
            if stage_filters:
                stage_task_ids = set()
                for stage_name in stage_filters:
                    stage_task_ids.update(
                        server.task_manager.stage_states.list_tasks_at_stage(
                            stage_name=stage_name,
                            state=stage_state,
                            project_id=resolved_project,
                        )
                    )

            tasks = server.task_manager.list_tasks(
                project_id=resolved_project,
                current_stage_state=current_stage_state,
                priority=priority,
                task_type=task_type,
                assignee=assignee,
                claimed=claimed,
                closed=closed,
                label=label,
                parent_task_id=parent_task_id,
                title_like=search,
                limit=10000 if stage_task_ids is not None else limit,
                offset=0 if stage_task_ids is not None else offset,
                sort_by=sort_by,
                sort_order=sort_order,
            )
            if stage_task_ids is not None:
                tasks = [task for task in tasks if task.id in stage_task_ids]
                total = len(tasks)
                tasks = tasks[offset : offset + limit]
            else:
                total = server.task_manager.count_tasks(project_id=resolved_project)

            task_dicts = [t.to_brief() for t in tasks]
            if include_stages or stage_filters or stage_state is not None:
                stages_by_task = _stage_views_for_tasks([task.id for task in tasks])
                for item in task_dicts:
                    item["stages"] = stages_by_task.get(item["id"], [])

            state_counts = server.task_manager.count_by_state(project_id=resolved_project)
            return {
                "tasks": task_dicts,
                "total": total,
                "stats": state_counts,
                "limit": limit,
                "offset": offset,
            }
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    # -----------------------------------------------------------------
    # CRUD
    # -----------------------------------------------------------------

    @router.post("", status_code=201)
    async def create_task(request_data: TaskCreateRequest) -> Any:
        """Create a new task."""
        try:
            project_id = _resolve_project(request_data.project_id)
            task = server.task_manager.create_task(
                project_id=project_id,
                title=request_data.title,
                description=request_data.description,
                priority=request_data.priority,
                task_type=request_data.task_type,
                parent_task_id=request_data.parent_task_id,
                labels=request_data.labels,
                category=request_data.category,
                validation_criteria=request_data.validation_criteria,
                assignee=request_data.assignee,
            )
            result = task.to_dict()
            await _broadcast_task("task_created", result)
            return result
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Failed to create task: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/{task_id}")
    async def get_task(task_id: str) -> Any:
        """Get a task by ID, seq_num (#N), or path (1.2.3)."""
        try:
            task = _resolve_task(task_id)
            return task.to_dict()
        except (ValueError, TaskNotFoundError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @router.patch("/{task_id}")
    async def update_task(task_id: str, request_data: TaskUpdateRequest) -> Any:
        """Update a task's fields. Only provided fields are changed."""
        try:
            # Resolve the task ID first
            try:
                task = _resolve_task(task_id)
            except (ValueError, TaskNotFoundError) as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
            resolved_id = task.id

            legacy_stage_key = "lifecycle_" + "stage"
            extra_fields = set(request_data.model_extra or {})
            legacy_fields = {"status", "lifecycle", legacy_stage_key} & extra_fields
            if legacy_fields:
                names = ", ".join(sorted(legacy_fields))
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unsupported legacy task field(s): {names}. Use stage-specific task "
                        "endpoints instead."
                    ),
                )
            if extra_fields:
                names = ", ".join(sorted(extra_fields))
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported task field(s): {names}.",
                )

            blocked_fields = request_data.model_fields_set & {"assignee"}
            if blocked_fields:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Use dedicated task endpoints instead of PATCH for ownership changes: "
                        "/claim, /release-claim, /escalate, /de-escalate, /close, /reopen, "
                        "or the stage PATCH route."
                    ),
                )

            # Build kwargs only for fields that were explicitly set
            kwargs: dict[str, Any] = {}
            for field_name in request_data.model_fields_set:
                kwargs[field_name] = getattr(request_data, field_name)
            if "isolation" in kwargs:
                if kwargs["isolation"] is None:
                    raise ValueError("isolation cannot be None")
                kwargs["isolation"] = validate_task_isolation_artifacts(
                    server.task_manager,
                    resolved_id,
                    cast(str, kwargs["isolation"]),
                )

            if not kwargs:
                return task.to_dict()

            updated = server.task_manager.update_task(resolved_id, **kwargs)
            result = updated.to_dict()
            await _broadcast_task("task_updated", result)
            return result
        except HTTPException:
            raise
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Failed to update task {task_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.delete("/{task_id}")
    async def delete_task(
        task_id: str,
        cascade: bool = Query(False, description="Delete children and dependents recursively"),
    ) -> dict[str, Any]:
        """Delete a task."""
        try:
            # Resolve first
            task = _resolve_task(task_id)
            resolved_id = task.id
            delete_result = server.task_manager.delete_task(resolved_id, cascade=cascade)
            if not delete_result:
                raise HTTPException(status_code=404, detail="Task not found")
            await _broadcast_task("task_deleted", {"id": resolved_id})
            return {"deleted": True, "id": resolved_id}
        except (ValueError, TaskNotFoundError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Failed to delete task {task_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # -----------------------------------------------------------------
    # Stage and ownership transitions
    # -----------------------------------------------------------------

    @router.post("/{task_id}/claim")
    async def claim_task(task_id: str, request_data: TaskClaimRequest) -> Any:
        """Claim a task for a session."""
        try:
            task = _resolve_task(task_id)
            resolved_id = task.id
            resolved_session_id = _resolve_session_ref(
                request_data.session_id,
                project_id=task.project_id,
            )
            claimed_task = server.task_manager.claim_task(
                resolved_id,
                session_id=resolved_session_id,
                force=request_data.force,
            )
            result = claimed_task.to_dict()
            await _broadcast_task("task_claimed", result)
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
        try:
            task = _resolve_task(task_id)
            resolved_id = task.id
            released = server.task_manager.release_task_claim(resolved_id)
            result = released.to_dict()
            await _broadcast_task("task_claim_released", result)
            return result
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/{task_id}/escalate")
    async def escalate_task(task_id: str, request_data: TaskEscalateRequest) -> Any:
        """Escalate a task without using generic mutation."""
        try:
            task = _resolve_task(task_id)
            resolved_id = task.id
            updated = server.task_manager.escalate_task(resolved_id, reason=request_data.reason)
            result = updated.to_dict()
            await _broadcast_task("task_escalated", result)
            return result
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/{task_id}/close")
    async def close_task(task_id: str, request_data: TaskCloseRequest | None = None) -> Any:
        """Close a task."""
        try:
            task = _resolve_task(task_id)
            resolved_id = task.id
            body = request_data or TaskCloseRequest()
            resolved_session_id = (
                _resolve_session_ref(body.session_id, project_id=task.project_id)
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
            await _broadcast_task("task_closed", result)
            return result
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/{task_id}/reopen")
    async def reopen_task(task_id: str, request_data: TaskReopenRequest | None = None) -> Any:
        """Reopen a closed task."""
        try:
            task = _resolve_task(task_id)
            resolved_id = task.id
            body = request_data or TaskReopenRequest()
            reopened = server.task_manager.reopen_task(resolved_id, reason=body.reason)
            result = reopened.to_dict()
            await _broadcast_task("task_reopened", result)
            return result
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/{task_id}/de-escalate")
    async def de_escalate_task(task_id: str, request_data: TaskDeEscalateRequest) -> Any:
        """De-escalate a task with user decision context."""
        try:
            task = _resolve_task(task_id)
            resolved_id = task.id
            updated = server.task_manager.de_escalate_task(
                resolved_id,
                reason=request_data.decision_context,
                reset_validation=request_data.reset_validation,
            )
            result = updated.to_dict()
            await _broadcast_task("task_de_escalated", result)
            return result
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    register_task_comment_routes(
        router,
        server,
        resolve_task=_resolve_task,
        broadcast_task=_broadcast_task,
    )
    register_task_dependency_routes(router, server, resolve_task=_resolve_task)

    return router
