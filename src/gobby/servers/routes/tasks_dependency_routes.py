"""Dependency routes for task HTTP endpoints."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class EnrichmentTaskView:
    id: str
    seq_num: int | None
    title: str
    task_type: str


def register_task_dependency_routes(
    router: APIRouter,
    server: HTTPServer,
    *,
    resolve_task: Callable[..., Task],
) -> None:
    """Register dependency routes on the shared tasks router."""

    async def _enrich_dependency_nodes(
        node: dict[str, Any],
        cache: dict[str, EnrichmentTaskView | None],
        *,
        depth: int,
        max_depth: int,
    ) -> None:
        """Attach the real task identity (ref/title/type) to each tree node.

        The storage tree carries ids only; the UI must show the actual tasks,
        not bare counts. Resolution is cached per request and bounded by the
        tree's own ``max_depth``, so the fan-out stays small.
        """
        node_id = node.get("id")
        if isinstance(node_id, str) and node_id:
            if node_id not in cache:
                try:
                    task = await server.run_db(server.task_manager.get_task, node_id)
                    cache[node_id] = (
                        EnrichmentTaskView(
                            id=task.id,
                            seq_num=task.seq_num,
                            title=task.title,
                            task_type=task.task_type,
                        )
                        if task is not None
                        else None
                    )
                except (ValueError, TaskNotFoundError):
                    cache[node_id] = None
            resolved = cache[node_id]
            if resolved is not None:
                node["ref"] = f"#{resolved.seq_num}" if resolved.seq_num else resolved.id[:8]
                node["title"] = resolved.title
                node["task_type"] = resolved.task_type
        if depth >= max_depth:
            return
        for key in ("blockers", "blocking"):
            children = node.get(key)
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        await _enrich_dependency_nodes(
                            child,
                            cache,
                            depth=depth + 1,
                            max_depth=max_depth,
                        )

    def _collect_dependency_node_ids(node: dict[str, Any], ids: set[str]) -> None:
        node_id = node.get("id")
        if isinstance(node_id, str) and node_id:
            ids.add(node_id)
        for key in ("blockers", "blocking"):
            children = node.get(key)
            if not isinstance(children, list):
                continue
            for child in children:
                if isinstance(child, dict):
                    _collect_dependency_node_ids(child, ids)

    def _load_dependency_tasks(ids: list[str]) -> dict[str, EnrichmentTaskView | None]:
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = server.task_manager.db.fetchall(
            f"SELECT id, seq_num, title, task_type FROM tasks WHERE id IN ({placeholders})",
            tuple(ids),
        )
        loaded = {row["id"]: row for row in rows}
        cache: dict[str, EnrichmentTaskView | None] = {}
        for task_id in ids:
            row = loaded.get(task_id)
            if row is None:
                cache[task_id] = None
                continue
            cache[task_id] = EnrichmentTaskView(
                id=row["id"],
                seq_num=row["seq_num"],
                title=row["title"],
                task_type=row["task_type"],
            )
        return cache

    async def _enrich_dependency_tree(tree: dict[str, Any], *, max_depth: int) -> None:
        ids: set[str] = set()
        _collect_dependency_node_ids(tree, ids)
        raw_cache = await server.run_db(_load_dependency_tasks, sorted(ids))
        await _enrich_dependency_nodes(tree, raw_cache, depth=0, max_depth=max_depth)

    @router.get("/{task_id}/dependencies")
    async def get_dependency_tree(
        task_id: str,
        direction: Literal["blockers", "blocking", "both"] = Query(
            "both", description="Tree direction"
        ),
        max_depth: int = Query(10, ge=1, le=50, description="Maximum tree depth"),
    ) -> Any:
        """Get the dependency tree for a task."""
        try:
            task = resolve_task(task_id)
            dep_manager = TaskDependencyManager(server.task_manager.db)
            tree = dep_manager.get_dependency_tree(
                task.id,
                direction=direction,
                max_depth=max_depth,
            )
            await _enrich_dependency_tree(tree, max_depth=max_depth)
            return tree
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
