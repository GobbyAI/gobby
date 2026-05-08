"""Dependency routes for task HTTP endpoints."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from gobby.storage.task_dependencies import (
    DependencyCycleError,
    TaskDependencyManager,
)
from gobby.storage.tasks._models import TaskNotFoundError

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer
    from gobby.storage.tasks._models import Task


class DependencyAddRequest(BaseModel):
    """Request body for adding a dependency."""

    depends_on: str = Field(..., description="Task ID that must complete first")
    dep_type: Literal["blocks", "related", "discovered-from"] = Field(
        default="blocks", description="Dependency type"
    )


def register_task_dependency_routes(
    router: APIRouter,
    server: HTTPServer,
    *,
    resolve_task: Callable[..., Task],
) -> None:
    """Register dependency routes on the shared tasks router."""

    @router.get("/{task_id}/dependencies")
    async def get_dependency_tree(
        task_id: str,
        direction: Literal["blockers", "blocking", "both"] = Query(
            "both", description="Tree direction"
        ),
    ) -> Any:
        """Get the dependency tree for a task."""
        try:
            task = resolve_task(task_id)
            dep_manager = TaskDependencyManager(server.task_manager.db)
            return dep_manager.get_dependency_tree(task.id, direction=direction)
        except (ValueError, TaskNotFoundError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @router.post("/{task_id}/dependencies", status_code=201)
    async def add_dependency(task_id: str, request_data: DependencyAddRequest) -> Any:
        """Add a dependency to a task."""
        try:
            task = resolve_task(task_id)
            blocker = resolve_task(request_data.depends_on, project_id=task.project_id)
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
        try:
            task = resolve_task(task_id)
            blocker = resolve_task(depends_on_id, project_id=task.project_id)
            dep_manager = TaskDependencyManager(server.task_manager.db)
            removed = dep_manager.remove_dependency(task.id, blocker.id)
            if not removed:
                raise HTTPException(status_code=404, detail="Dependency not found")
            return {"removed": True, "task_id": task.id, "depends_on": blocker.id}
        except (ValueError, TaskNotFoundError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
