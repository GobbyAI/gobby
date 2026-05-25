"""Project management API routes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from gobby.config.validation_detection import (
    clear_project_validation_detection,
    load_project_validation_detection,
    save_project_validation_detection,
)
from gobby.servers.tool_approvals import (
    clear_project_approval_rules,
    load_project_approval_rules,
    migrate_project_approval_rules,
    save_project_approval_rules,
)
from gobby.storage.github_triage import GitHubTriageConfig, GitHubTriageStore
from gobby.storage.projects import SYSTEM_PROJECT_NAMES, LocalProjectManager, Project

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
    linear_project_id: str | None = None
    approval_rules: list[str] | None = None
    validation_detection: dict[str, Any] | None = None


class GitHubTriageConfigUpdate(BaseModel):
    """Request body for project GitHub triage config."""

    enabled: bool | None = None
    webhook_enabled: bool | None = None
    repositories: list[str] | None = None
    reconcile_interval_seconds: int | None = None
    webhook_secret_ref: str | None = None


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


def _project_to_response(server: HTTPServer, project: Project) -> dict[str, Any]:
    data = project.to_dict()
    data["display_name"] = "Personal" if project.name == "_personal" else project.name
    data.update(_get_project_stats(server, project.id))
    data["approval_rules"] = (
        load_project_approval_rules(project.repo_path) if project.repo_path else []
    )
    data["validation_detection"] = (
        load_project_validation_detection(project.repo_path) if project.repo_path else None
    )
    return data


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

            results.append(_project_to_response(server, project))

        return results

    @router.get("/{project_id}")
    async def get_project(project_id: str) -> dict[str, Any]:
        """Get a single project with stats."""
        pm = _get_project_manager(server)
        project = pm.get(project_id)
        if not project or project.deleted_at:
            raise HTTPException(404, "Project not found")

        return _project_to_response(server, project)

    @router.put("/{project_id}")
    async def update_project(project_id: str, body: ProjectUpdate) -> dict[str, Any]:
        """Update project fields."""
        pm = _get_project_manager(server)
        project = pm.get(project_id)
        if not project or project.deleted_at:
            raise HTTPException(404, "Project not found")

        fields = body.model_dump(exclude_none=True)
        approval_rules = fields.pop("approval_rules", None)
        validation_detection = fields.pop("validation_detection", None)
        original_repo_path = project.repo_path
        requested_repo_path = fields.get("repo_path", original_repo_path)
        repo_path_changed = requested_repo_path != original_repo_path
        migrated_rules = (
            load_project_approval_rules(original_repo_path)
            if repo_path_changed and original_repo_path
            else []
        )
        migrated_validation_detection = (
            load_project_validation_detection(original_repo_path)
            if repo_path_changed and original_repo_path
            else None
        )
        if not fields:
            if approval_rules is None and validation_detection is None:
                return _project_to_response(server, project)

        if fields:
            updated = pm.update(project_id, **fields)
            if not updated:
                raise HTTPException(500, "Failed to update project")
        else:
            updated = project

        if approval_rules is not None:
            if not updated.repo_path:
                raise HTTPException(
                    400, "Project has no repo_path for project-scoped approval rules"
                )
            if repo_path_changed and original_repo_path:
                migrate_project_approval_rules(
                    original_repo_path, updated.repo_path, approval_rules
                )
            else:
                save_project_approval_rules(updated.repo_path, approval_rules)
        elif repo_path_changed and updated.repo_path and migrated_rules:
            migrate_project_approval_rules(original_repo_path, updated.repo_path)

        if validation_detection is not None:
            if not updated.repo_path:
                raise HTTPException(
                    400, "Project has no repo_path for project-scoped validation detection"
                )
            try:
                save_project_validation_detection(updated.repo_path, validation_detection)
            except ValidationError as exc:
                raise HTTPException(400, str(exc)) from exc
        elif repo_path_changed and updated.repo_path and migrated_validation_detection is not None:
            save_project_validation_detection(updated.repo_path, migrated_validation_detection)

        if (
            repo_path_changed
            and original_repo_path
            and (approval_rules is not None or migrated_rules)
        ):
            clear_project_approval_rules(original_repo_path)
        if (
            repo_path_changed
            and original_repo_path
            and (validation_detection is not None or migrated_validation_detection is not None)
        ):
            clear_project_validation_detection(original_repo_path)

        return _project_to_response(server, updated)

    @router.get("/{project_id}/github-triage")
    async def get_github_triage_config(project_id: str) -> dict[str, Any]:
        """Get GitHub issue triage configuration for a project."""
        pm = _get_project_manager(server)
        project = pm.get(project_id)
        if not project or project.deleted_at:
            raise HTTPException(404, "Project not found")
        config = GitHubTriageStore(server.services.database).get_config(
            project_id,
            fallback_repo=project.github_repo,
        )
        return config.to_dict()

    @router.put("/{project_id}/github-triage")
    async def update_github_triage_config(
        project_id: str,
        body: GitHubTriageConfigUpdate,
    ) -> dict[str, Any]:
        """Update GitHub issue triage configuration for a project."""
        pm = _get_project_manager(server)
        project = pm.get(project_id)
        if not project or project.deleted_at:
            raise HTTPException(404, "Project not found")

        store = GitHubTriageStore(server.services.database)
        current = store.get_config(project_id, fallback_repo=project.github_repo)
        values = body.model_dump(exclude_unset=True)
        interval = values.get(
            "reconcile_interval_seconds",
            current.reconcile_interval_seconds,
        )
        if interval is not None and interval <= 0:
            raise HTTPException(400, "reconcile_interval_seconds must be greater than 0")

        updated = store.upsert_config(
            GitHubTriageConfig(
                project_id=project_id,
                enabled=values.get("enabled", current.enabled),
                webhook_enabled=values.get("webhook_enabled", current.webhook_enabled),
                repositories=tuple(values.get("repositories", current.repositories)),
                reconcile_interval_seconds=interval,
                webhook_secret_ref=values.get("webhook_secret_ref", current.webhook_secret_ref),
            )
        )
        return updated.to_dict()

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
