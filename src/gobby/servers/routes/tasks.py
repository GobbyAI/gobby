"""
Task routes for Gobby HTTP server.

Provides CRUD, list, lifecycle, and dependency endpoints for the task system.
"""

import logging
import uuid
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from gobby.storage.task_dependencies import (
    DependencyCycleError,
    TaskDependencyManager,
)
from gobby.storage.tasks._models import VALID_CATEGORIES, TaskNotFoundError
from gobby.utils.metrics import get_metrics_collector

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

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
        default="task", description="Task type (task, bug, feature, epic, chore)"
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


class TaskUpdateRequest(BaseModel):
    """Request body for updating a task."""

    title: str | None = Field(default=None, description="New title")
    description: str | None = Field(default=None, description="New description")
    status: str | None = Field(default=None, description="New status")
    priority: int | None = Field(default=None, description="New priority")
    task_type: str | None = Field(default=None, description="New task type")
    assignee: str | None = Field(default=None, description="New assignee")
    labels: list[str] | None = Field(default=None, description="New labels")
    parent_task_id: str | None = Field(default=None, description="New parent task ID")
    category: str | None = Field(default=None, description="New category")
    validation_criteria: str | None = Field(default=None, description="New validation criteria")
    sequence_order: int | None = Field(default=None, description="Sort order within column/group")


class TaskCloseRequest(BaseModel):
    """Request body for closing a task."""

    reason: str | None = Field(default=None, description="Reason for closing")
    commit_sha: str | None = Field(default=None, description="Git commit SHA to link")
    session_id: str | None = Field(default=None, description="Session that closed the task")


class TaskReopenRequest(BaseModel):
    """Request body for reopening a task."""

    reason: str | None = Field(default=None, description="Reason for reopening")


class TaskDeEscalateRequest(BaseModel):
    """Request body for de-escalating a task."""

    decision_context: str = Field(..., description="User's decision or instructions for the agent")
    reset_validation: bool = Field(default=False, description="Also reset validation fail count")


class TaskCommentCreateRequest(BaseModel):
    """Request body for creating a comment."""

    body: str = Field(..., description="Comment body (markdown)")
    author: str = Field(..., description="Author ID (session or user)")
    author_type: str = Field(default="session", description="Author type: session, agent, human")
    parent_comment_id: str | None = Field(
        default=None, description="Parent comment ID for threading"
    )


class DependencyAddRequest(BaseModel):
    """Request body for adding a dependency."""

    depends_on: str = Field(..., description="Task ID that must complete first")
    dep_type: Literal["blocks", "related", "discovered-from"] = Field(
        default="blocks", description="Dependency type"
    )


# =============================================================================
# Router
# =============================================================================


def create_tasks_router(server: "HTTPServer") -> APIRouter:
    """Create tasks router with endpoints bound to server instance."""
    router = APIRouter(prefix="/api/tasks", tags=["tasks"])
    metrics = get_metrics_collector()

    def _resolve_project(project_id: str | None) -> str:
        """Resolve project ID, falling back to server's project context."""
        if project_id:
            return project_id
        return server.resolve_project_id(project_id=None, cwd=None)

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

    # -----------------------------------------------------------------
    # List / Stats
    # -----------------------------------------------------------------

    @router.get("")
    async def list_tasks(
        project_id: str | None = Query(None, description="Filter by project ID"),
        status: str | None = Query(None, description="Filter by status"),
        priority: int | None = Query(None, description="Filter by priority"),
        task_type: str | None = Query(None, description="Filter by task type"),
        assignee: str | None = Query(None, description="Filter by assignee"),
        label: str | None = Query(None, description="Filter by label"),
        parent_task_id: str | None = Query(None, description="Filter by parent task ID"),
        search: str | None = Query(None, description="Search by title"),
        limit: int = Query(50, description="Maximum results"),
        offset: int = Query(0, description="Pagination offset"),
    ) -> dict[str, Any]:
        """List tasks with optional filters and status distribution stats."""
        metrics.inc_counter("http_requests_total")
        try:
            resolved_project = _resolve_project(project_id)

            # Parse comma-separated status values
            status_filter: str | list[str] | None = None
            if status and "," in status:
                status_filter = [s.strip() for s in status.split(",")]
            else:
                status_filter = status

            tasks = server.task_manager.list_tasks(
                project_id=resolved_project,
                status=status_filter,
                priority=priority,
                task_type=task_type,
                assignee=assignee,
                label=label,
                parent_task_id=parent_task_id,
                title_like=search,
                limit=limit,
                offset=offset,
            )
            status_counts = server.task_manager.count_by_status(project_id=resolved_project)
            total = server.task_manager.count_tasks(project_id=resolved_project)
            return {
                "tasks": [t.to_brief() for t in tasks],
                "total": total,
                "stats": status_counts,
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
        metrics.inc_counter("http_requests_total")
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
        metrics.inc_counter("http_requests_total")
        try:
            task = server.task_manager.get_task(task_id)
            return task.to_dict()
        except (ValueError, TaskNotFoundError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @router.patch("/{task_id}")
    async def update_task(task_id: str, request_data: TaskUpdateRequest) -> Any:
        """Update a task's fields. Only provided fields are changed."""
        metrics.inc_counter("http_requests_total")
        try:
            # Resolve the task ID first
            task = server.task_manager.get_task(task_id)
            resolved_id = task.id

            # Build kwargs only for fields that were explicitly set
            kwargs: dict[str, Any] = {}
            for field_name in request_data.model_fields_set:
                kwargs[field_name] = getattr(request_data, field_name)

            if not kwargs:
                return task.to_dict()

            updated = server.task_manager.update_task(resolved_id, **kwargs)
            result = updated.to_dict()
            await _broadcast_task("task_updated", result)
            return result
        except (ValueError, TaskNotFoundError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Failed to update task {task_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.delete("/{task_id}")
    async def delete_task(
        task_id: str,
        cascade: bool = Query(False, description="Delete children and dependents recursively"),
    ) -> dict[str, Any]:
        """Delete a task."""
        metrics.inc_counter("http_requests_total")
        try:
            # Resolve first
            task = server.task_manager.get_task(task_id)
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
    # Lifecycle
    # -----------------------------------------------------------------

    @router.post("/{task_id}/close")
    async def close_task(task_id: str, request_data: TaskCloseRequest | None = None) -> Any:
        """Close a task."""
        metrics.inc_counter("http_requests_total")
        try:
            task = server.task_manager.get_task(task_id)
            resolved_id = task.id
            body = request_data or TaskCloseRequest()

            if body.commit_sha:
                server.task_manager.link_commit(resolved_id, body.commit_sha)

            closed = server.task_manager.close_task(
                resolved_id,
                reason=body.reason,
                closed_in_session_id=body.session_id,
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
        metrics.inc_counter("http_requests_total")
        try:
            task = server.task_manager.get_task(task_id)
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
        """De-escalate a task and return it to open status with user decision context."""
        metrics.inc_counter("http_requests_total")
        try:
            task = server.task_manager.get_task(task_id)
            resolved_id = task.id

            if task.status != "escalated":
                raise ValueError(f"Task is not escalated (status: {task.status})")

            update_kwargs: dict[str, Any] = {
                "status": "in_progress",
                "escalated_at": None,
                "escalation_reason": None,
            }

            if request_data.reset_validation:
                update_kwargs["validation_fail_count"] = 0

            # Append user decision context to description
            decision_note = f"\n\n---\n**User decision:** {request_data.decision_context}"
            current_desc = task.description or ""
            update_kwargs["description"] = current_desc + decision_note

            updated = server.task_manager.update_task(resolved_id, **update_kwargs)
            result = updated.to_dict()
            await _broadcast_task("task_de_escalated", result)
            return result
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    # -----------------------------------------------------------------
    # Comments
    # -----------------------------------------------------------------

    @router.get("/{task_id}/comments")
    async def list_comments(
        task_id: str,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> Any:
        """List comments for a task, threaded."""
        metrics.inc_counter("http_requests_total")
        try:
            task = server.task_manager.get_task(task_id)
            resolved_id = task.id

            total_row = server.task_manager.db.fetchone(
                "SELECT COUNT(*) as total FROM task_comments WHERE task_id = ?",
                (resolved_id,),
            )
            total = total_row["total"] if total_row else 0

            rows = server.task_manager.db.fetchall(
                """SELECT id, task_id, parent_comment_id, author, author_type, body,
                          created_at, updated_at
                   FROM task_comments
                   WHERE task_id = ?
                   ORDER BY created_at ASC
                   LIMIT ? OFFSET ?""",
                (resolved_id, limit, offset),
            )
            comments = [dict(row) for row in rows]
            return {"comments": comments, "count": len(comments), "total": total}
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/{task_id}/comments")
    async def create_comment(task_id: str, request_data: TaskCommentCreateRequest) -> Any:
        """Add a comment to a task."""
        metrics.inc_counter("http_requests_total")
        try:
            task = server.task_manager.get_task(task_id)
            resolved_id = task.id

            comment_id = str(uuid.uuid4())
            server.task_manager.db.execute(
                """INSERT INTO task_comments (id, task_id, parent_comment_id, author, author_type, body)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    comment_id,
                    resolved_id,
                    request_data.parent_comment_id,
                    request_data.author,
                    request_data.author_type,
                    request_data.body,
                ),
            )

            row = server.task_manager.db.fetchone(
                "SELECT * FROM task_comments WHERE id = ?", (comment_id,)
            )
            result = dict(row) if row else {"id": comment_id}
            await _broadcast_task("task_comment_added", {**result, "task_ref": task.ref})
            return result
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.delete("/{task_id}/comments/{comment_id}")
    async def delete_comment(task_id: str, comment_id: str) -> Any:
        """Delete a comment."""
        metrics.inc_counter("http_requests_total")
        try:
            task = server.task_manager.get_task(task_id)
            server.task_manager.db.execute(
                "DELETE FROM task_comments WHERE id = ? AND task_id = ?",
                (comment_id, task.id),
            )
            return {"deleted": True}
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    # -----------------------------------------------------------------
    # Dependencies
    # -----------------------------------------------------------------

    @router.get("/{task_id}/dependencies")
    async def get_dependency_tree(
        task_id: str,
        direction: Literal["blockers", "blocking", "both"] = Query(
            "both", description="Tree direction"
        ),
    ) -> Any:
        """Get the dependency tree for a task."""
        metrics.inc_counter("http_requests_total")
        try:
            task = server.task_manager.get_task(task_id)
            dep_manager = TaskDependencyManager(server.task_manager.db)
            return dep_manager.get_dependency_tree(task.id, direction=direction)
        except (ValueError, TaskNotFoundError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @router.post("/{task_id}/dependencies", status_code=201)
    async def add_dependency(task_id: str, request_data: DependencyAddRequest) -> Any:
        """Add a dependency to a task."""
        metrics.inc_counter("http_requests_total")
        try:
            task = server.task_manager.get_task(task_id)
            blocker = server.task_manager.get_task(request_data.depends_on)
            dep_manager = TaskDependencyManager(server.task_manager.db)
            dep = dep_manager.add_dependency(task.id, blocker.id, dep_type=request_data.dep_type)
            return dep.to_dict()
        except DependencyCycleError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except (ValueError, TaskNotFoundError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.delete("/{task_id}/dependencies/{depends_on_id}")
    async def remove_dependency(task_id: str, depends_on_id: str) -> dict[str, Any]:
        """Remove a dependency from a task."""
        metrics.inc_counter("http_requests_total")
        try:
            task = server.task_manager.get_task(task_id)
            blocker = server.task_manager.get_task(depends_on_id)
            dep_manager = TaskDependencyManager(server.task_manager.db)
            removed = dep_manager.remove_dependency(task.id, blocker.id)
            if not removed:
                raise HTTPException(status_code=404, detail="Dependency not found")
            return {"removed": True, "task_id": task.id, "depends_on": blocker.id}
        except (ValueError, TaskNotFoundError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    return router
