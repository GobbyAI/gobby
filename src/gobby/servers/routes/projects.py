"""Project management API routes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from gobby.storage.projects import SYSTEM_PROJECT_NAMES, LocalProjectManager

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

HIDDEN_PROJECT_NAMES = frozenset({"_orphaned", "_migrated", "_global"})


class ProjectUpdate(BaseModel):
    """Request body for updating a project."""

    name: str | None = None
    repo_path: str | None = None
    github_url: str | None = None
    github_repo: str | None = None
    linear_team_id: str | None = None


def _get_project_manager(server: HTTPServer) -> LocalProjectManager:
    """Get a LocalProjectManager from the server's database."""
    if server.session_manager is None:
        raise HTTPException(503, "Session manager not available")
    return LocalProjectManager(server.session_manager.db)


def _get_project_stats(server: HTTPServer, project_id: str) -> dict[str, Any]:
    """Get computed stats for a project."""
    if server.session_manager is None:
        return {"session_count": 0, "open_task_count": 0, "last_activity_at": None}

    db = server.session_manager.db

    session_count = db.fetchone(
        "SELECT COUNT(*) as cnt FROM sessions WHERE project_id = ? AND status IN ('active', 'paused')",
        (project_id,),
    )

    open_task_count = db.fetchone(
        "SELECT COUNT(*) as cnt FROM tasks WHERE project_id = ? AND closed_at IS NULL",
        (project_id,),
    )

    last_activity = db.fetchone(
        "SELECT MAX(updated_at) as last_activity FROM sessions WHERE project_id = ?",
        (project_id,),
    )

    return {
        "session_count": session_count["cnt"] if session_count else 0,
        "open_task_count": open_task_count["cnt"] if open_task_count else 0,
        "last_activity_at": last_activity["last_activity"] if last_activity else None,
    }


def create_projects_router(server: HTTPServer) -> APIRouter:
    """Create the projects API router."""
    router = APIRouter(prefix="/api/projects", tags=["projects"])

    @router.get("")
    async def list_projects() -> list[dict[str, Any]]:
        """List all projects with computed stats."""
        pm = _get_project_manager(server)
        projects = pm.list()

        results = []
        for project in projects:
            if project.name in HIDDEN_PROJECT_NAMES:
                continue

            data = project.to_dict()
            data["display_name"] = "Personal" if project.name == "_personal" else project.name
            data.update(_get_project_stats(server, project.id))
            results.append(data)

        return results

    @router.get("/{project_id}")
    async def get_project(project_id: str) -> dict[str, Any]:
        """Get a single project with stats."""
        pm = _get_project_manager(server)
        project = pm.get(project_id)
        if not project or project.deleted_at:
            raise HTTPException(404, "Project not found")

        data = project.to_dict()
        data["display_name"] = "Personal" if project.name == "_personal" else project.name
        data.update(_get_project_stats(server, project.id))
        return data

    @router.put("/{project_id}")
    async def update_project(project_id: str, body: ProjectUpdate) -> dict[str, Any]:
        """Update project fields."""
        pm = _get_project_manager(server)
        project = pm.get(project_id)
        if not project or project.deleted_at:
            raise HTTPException(404, "Project not found")

        fields = body.model_dump(exclude_none=True)
        if not fields:
            data = project.to_dict()
            data["display_name"] = "Personal" if project.name == "_personal" else project.name
            data.update(_get_project_stats(server, project.id))
            return data

        updated = pm.update(project_id, **fields)
        if not updated:
            raise HTTPException(500, "Failed to update project")

        data = updated.to_dict()
        data["display_name"] = "Personal" if updated.name == "_personal" else updated.name
        data.update(_get_project_stats(server, project_id))
        return data

    @router.delete("/{project_id}")
    async def delete_project(project_id: str) -> dict[str, str]:
        """Soft-delete a project. Protected projects cannot be deleted."""
        pm = _get_project_manager(server)
        project = pm.get(project_id)
        if not project or project.deleted_at:
            raise HTTPException(404, "Project not found")

        if project.name in SYSTEM_PROJECT_NAMES:
            raise HTTPException(403, f"Cannot delete protected project '{project.name}'")

        if not pm.soft_delete(project_id):
            raise HTTPException(500, "Failed to delete project")

        return {"status": "deleted", "id": project_id}

    return router
