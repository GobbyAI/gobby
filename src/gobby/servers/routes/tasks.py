"""
Task routes for Gobby HTTP server.

Provides CRUD, list, stage-transition, and dependency endpoints for the task system.
"""

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gobby.build.lifecycle import derive_build_state
from gobby.servers.routes.tasks_comment_routes import register_task_comment_routes
from gobby.servers.routes.tasks_dependency_routes import register_task_dependency_routes
from gobby.servers.routes.tasks_lifecycle_routes import register_task_lifecycle_routes
from gobby.servers.routes.tasks_stage_routes import register_task_stage_routes
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks._models import (
    TASK_TYPE_CHOICES,
    VALID_CATEGORIES,
    TaskHasChildrenError,
    TaskHasDependentsError,
    TaskNotFoundError,
    validate_task_type,
)
from gobby.storage.tasks._queries import task_read_snapshot
from gobby.storage.tasks._stage_types import StageState
from gobby.storage.tasks._stage_views import stage_state_view
from gobby.tasks.isolation import validate_task_isolation_artifacts

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer
    from gobby.storage.tasks._models import Task

logger = logging.getLogger(__name__)


def _enum_schema(values: Iterable[str]) -> Any:
    return {"enum": list(values)}


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
        json_schema_extra=_enum_schema(TASK_TYPE_CHOICES),
    )
    parent_task_id: str | None = Field(default=None, description="Parent task ID")
    labels: list[str] | None = Field(default=None, description="Labels for categorization")
    category: str | None = Field(
        default=None,
        description=f"Task domain: {', '.join(sorted(VALID_CATEGORIES))}",
    )
    validation_criteria: str | None = Field(default=None, description="Acceptance criteria")
    implementation_domain: Literal["backend", "frontend", "fullstack"] | None = Field(
        default=None,
        description="Required for code tasks; routes implementation to the matching developer.",
    )
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
        json_schema_extra=_enum_schema(TASK_TYPE_CHOICES),
    )
    labels: list[str] | None = Field(default=None, description="New labels")
    parent_task_id: str | None = Field(default=None, description="New parent task ID")
    category: str | None = Field(default=None, description="New category")
    validation_criteria: str | None = Field(default=None, description="New validation criteria")
    implementation_domain: Literal["backend", "frontend", "fullstack"] | None = Field(
        default=None,
        description="Code task implementation domain.",
    )
    affected_files: list[str] = Field(
        default_factory=list,
        description="Replacement declared file scope. An empty array clears it.",
    )
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


# =============================================================================
# Router
# =============================================================================


def create_tasks_router(server: "HTTPServer") -> APIRouter:
    """Create tasks router with endpoints bound to server instance."""
    router = APIRouter(prefix="/api/tasks", tags=["tasks"])

    def _resolve_project(project_id: str | None) -> str:
        """Resolve project ID, falling back to server's project context."""
        resolved_project_id = project_id or server.resolve_project_id(project_id=None, cwd=None)
        if LocalProjectManager(server.task_manager.db).get(resolved_project_id) is None:
            raise ValueError(f"Project not found: {resolved_project_id}")
        return resolved_project_id

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

    async def _broadcast_task_or_raise(event: str, task_dict: dict[str, Any]) -> None:
        """Broadcast a task event via WebSocket if available."""
        ws = server.services.websocket_server
        if ws:
            await server.run_db(_apply_owner_ref, [task_dict])
            await ws.broadcast_task_event(event, task_id=task_dict.get("id", ""), task=task_dict)

    async def _broadcast_task(event: str, task_dict: dict[str, Any]) -> None:
        """Best-effort task broadcast for routes where HTTP already reports success."""
        try:
            await _broadcast_task_or_raise(event, task_dict)
        except Exception as e:
            logger.debug("Failed to broadcast task event %s: %s", event, e)

    def _resolve_session_ref(session_ref: str, project_id: str | None) -> str:
        """Resolve session references to canonical UUIDs before storage writes."""
        if server.session_manager is None:
            return session_ref
        return str(server.session_manager.resolve_session_reference(session_ref, project_id))

    def _stage_view(stage: StageState) -> dict[str, Any]:
        return stage_state_view(stage)

    def _stage_views_for_tasks(task_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not task_ids:
            return {}
        placeholders = ", ".join("%s" for _ in task_ids)
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

    def _apply_build_state(task_dicts: list[dict[str, Any]]) -> None:
        """Attach a definitive build_state to each serialized task.

        Derived from allow_automation + the durable ``gobby build`` lifecycle
        event — never from planning scaffolding (stages/agent/isolation) or
        dispatch_failure_count, which misclassify a cleanly stopped build.
        """
        if not task_dicts:
            return
        ids = [item["id"] for item in task_dicts]
        built = server.task_manager.lifecycle_events.tasks_with_build_event(ids)
        for item in task_dicts:
            item["build_state"] = derive_build_state(
                allow_automation=bool(item.get("allow_automation")),
                has_build_event=item["id"] in built,
            )

    def _apply_owner_ref(task_dicts: list[dict[str, Any]]) -> None:
        """Attach a friendly ``owner_session_ref`` to each serialized task.

        ``state.owner_session_id`` is authoritative because it mirrors the
        canonical task state. Serialized task rows also expose top-level
        ``claimed_by_session_id`` for compact consumers, so use it as a fallback.
        Owner UUIDs are resolved in one query; unowned tasks or non-string
        owner values receive ``None``.
        """
        owners_by_task: dict[str, str] = {}
        for item in task_dicts:
            raw_state = item.get("state")
            owner_id: Any = None
            if isinstance(raw_state, dict):
                owner_id = raw_state.get("owner_session_id")
            if not owner_id:
                owner_id = item.get("claimed_by_session_id")
            if isinstance(owner_id, str):
                owners_by_task[item["id"]] = owner_id

        sessions_by_id: dict[str, Any] = {}
        owner_ids = sorted(set(owners_by_task.values()))
        if owner_ids and server.session_manager is not None:
            try:
                placeholders = ",".join("%s" for _ in owner_ids)
                rows = server.session_manager.db.fetchall(
                    f"SELECT id, seq_num, source FROM sessions WHERE id IN ({placeholders})",  # nosec B608
                    tuple(owner_ids),
                )
                sessions_by_id = {row["id"]: row for row in rows}
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Failed to batch-resolve owner sessions: %s", exc)

        for item in task_dicts:
            owner_id = owners_by_task.get(item["id"])
            if owner_id is None:
                item["owner_session_ref"] = None
                continue
            session = sessions_by_id.get(owner_id)
            item["owner_session_ref"] = {
                "session_id": owner_id,
                "ref": f"#{session['seq_num']}" if session and session["seq_num"] else owner_id[:8],
                "source": session["source"] if session else None,
            }

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
        resolve_session_ref=_resolve_session_ref,
        broadcast_task=_broadcast_task,
        stage_view=_stage_view,
    )
    register_task_lifecycle_routes(
        router,
        server,
        resolve_task=_resolve_task,
        resolve_session_ref=_resolve_session_ref,
        broadcast_task=_broadcast_task,
        broadcast_claim_task=_broadcast_task_or_raise,
    )

    # -----------------------------------------------------------------
    # List / Stats
    # -----------------------------------------------------------------

    @router.get("")
    async def list_tasks(
        project_id: str | None = Query(None, description="Filter by project ID"),
        current_stage_state: (
            Literal["ready", "in_progress", "needs_review", "review_approved"] | None
        ) = Query(None, description="Filter by current stage state"),
        claimed: bool | None = Query(None, description="Filter by whether the task is claimed"),
        closed: bool | None = Query(None, description="Filter by canonical closed state"),
        priority: int | None = Query(None, description="Filter by priority"),
        task_type: str | None = Query(None, description="Filter by task type"),
        label: str | None = Query(None, description="Filter by label"),
        parent_task_id: str | None = Query(None, description="Filter by parent task ID"),
        search: str | None = Query(None, description="Search by title"),
        limit: int = Query(50, ge=1, le=1000, description="Maximum results"),
        offset: int = Query(0, ge=0, description="Pagination offset"),
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
            resolved_project = await server.run_db(_resolve_project, project_id)
            stage_filters = _normalize_stage_filters(stage)
            if stage_state is not None and not stage_filters:
                raise HTTPException(status_code=400, detail="stage_state requires stage")

            filters: dict[str, Any] = {
                "project_id": resolved_project,
                "current_stage_state": current_stage_state,
                "priority": priority,
                "task_type": task_type,
                "claimed": claimed,
                "closed": closed,
                "label": label,
                "parent_task_id": parent_task_id,
                "title_like": search,
                "stages": stage_filters,
                "stage_state": stage_state,
            }

            def _page_and_total() -> tuple[list[Task], int]:
                """Read the page and its total from one snapshot.

                Two transactions let a close land between them, so the page
                shrinks while ``total`` still counts the task and the UI's
                load-more drifts (#20870 F2).
                """
                with task_read_snapshot(server.task_manager.db):
                    return (
                        server.task_manager.list_tasks(
                            **filters,
                            limit=limit,
                            offset=offset,
                            sort_by=sort_by,
                            sort_order=sort_order,
                        ),
                        server.task_manager.count_tasks(**filters),
                    )

            tasks, total = await server.run_db(_page_and_total)

            task_dicts = [t.to_brief() for t in tasks]
            if include_stages or stage_filters or stage_state is not None:
                stages_by_task = await server.run_db(
                    _stage_views_for_tasks, [task.id for task in tasks]
                )
                for item in task_dicts:
                    item["stages"] = stages_by_task.get(item["id"], [])
            await server.run_db(_apply_build_state, task_dicts)
            await server.run_db(_apply_owner_ref, task_dicts)

            state_counts = await server.run_db(
                server.task_manager.count_by_state, project_id=resolved_project
            )
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
            project_id = await server.run_db(_resolve_project, request_data.project_id)
            if request_data.category == "code" and request_data.implementation_domain is None:
                raise ValueError("Code tasks require implementation_domain.")
            task = await server.run_db(
                server.task_manager.create_task,
                project_id=project_id,
                title=request_data.title,
                description=request_data.description,
                priority=request_data.priority,
                task_type=request_data.task_type,
                parent_task_id=request_data.parent_task_id,
                labels=request_data.labels,
                category=request_data.category,
                validation_criteria=request_data.validation_criteria,
                implementation_domain=request_data.implementation_domain,
            )
            result = task.to_dict()
            await _broadcast_task("task_created", result)
            return result
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception("Failed to create task: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.get("/{task_id}")
    async def get_task(task_id: str) -> Any:
        """Get a task by ID, seq_num (#N), or path (1.2.3)."""
        try:
            task = await server.run_db(_resolve_task, task_id)
            data = task.to_dict()
            data["build_state"] = derive_build_state(
                allow_automation=bool(task.allow_automation),
                has_build_event=await server.run_db(
                    server.task_manager.lifecycle_events.has_build_event, task.id
                ),
            )
            await server.run_db(_apply_owner_ref, [data])
            return data
        except (ValueError, TaskNotFoundError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @router.patch("/{task_id}")
    async def update_task(task_id: str, request_data: TaskUpdateRequest) -> Any:
        """Update a task's fields. Only provided fields are changed."""
        try:
            # Resolve the task ID first
            try:
                task = await server.run_db(_resolve_task, task_id)
            except (ValueError, TaskNotFoundError) as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
            resolved_id = task.id

            extra_fields = set(request_data.model_extra or {})
            if extra_fields:
                names = ", ".join(sorted(extra_fields))
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported task field(s): {names}.",
                )

            # Build kwargs only for fields that were explicitly set
            kwargs: dict[str, Any] = {}
            for field_name in request_data.model_fields_set:
                kwargs[field_name] = getattr(request_data, field_name)
            if "isolation" in kwargs:
                if kwargs["isolation"] is None:
                    kwargs.pop("isolation")
                else:
                    kwargs["isolation"] = await server.run_db(
                        validate_task_isolation_artifacts,
                        server.task_manager,
                        resolved_id,
                        cast(str, kwargs["isolation"]),
                    )

            if not kwargs:
                unchanged = task.to_dict()
                await server.run_db(_apply_owner_ref, [unchanged])
                return unchanged

            updated = await server.run_db(server.task_manager.update_task, resolved_id, **kwargs)
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
            logger.exception("Failed to update task %s: %s", task_id, e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.delete("/{task_id}")
    async def delete_task(
        task_id: str,
        cascade: bool = Query(False, description="Delete children and dependents recursively"),
    ) -> dict[str, Any]:
        """Delete a task."""
        try:
            # Resolve first
            task = await server.run_db(_resolve_task, task_id)
            resolved_id = task.id
            delete_result = await server.run_db(
                server.task_manager.delete_task, resolved_id, cascade=cascade
            )
            if not delete_result:
                raise HTTPException(status_code=404, detail="Task not found")
            await _broadcast_task("task_deleted", {"id": resolved_id})
            return {"deleted": True, "id": resolved_id}
        except HTTPException:
            raise
        except (TaskHasChildrenError, TaskHasDependentsError) as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except (ValueError, TaskNotFoundError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Failed to delete task %s: %s", task_id, e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    register_task_comment_routes(
        router,
        server,
        resolve_task=_resolve_task,
        broadcast_task=_broadcast_task,
    )
    register_task_dependency_routes(router, server, resolve_task=_resolve_task)

    return router
