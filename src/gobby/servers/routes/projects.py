"""Project management API routes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, ValidationError

from gobby.config.validation_detection import (
    ValidationDetectionConfig,
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

    enabled: bool = False
    webhook_enabled: bool = False
    repositories: list[str] = Field(default_factory=list)
    reconcile_interval_seconds: int = 3600
    webhook_secret_ref: str | None = None


def _get_project_manager(server: HTTPServer) -> LocalProjectManager:
    """Get a LocalProjectManager from the server's database."""
    if server.session_manager is None:
        raise HTTPException(503, "Session manager not available")
    return LocalProjectManager(server.session_manager.db)


def _get_project_stats_batch(
    server: HTTPServer, project_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Get computed stats for projects in two bounded queries."""
    stats = {
        project_id: {"session_count": 0, "open_task_count": 0, "last_activity_at": None}
        for project_id in project_ids
    }
    if server.session_manager is None:
        return stats
    if not project_ids:
        return stats

    db = server.session_manager.db
    placeholders = ",".join("%s" for _ in project_ids)
    session_rows = db.fetchall(
        f"""
        SELECT project_id,
               COUNT(*) FILTER (WHERE status IN ('active', 'paused')) AS session_count,
               MAX(updated_at) AS last_activity_at
        FROM sessions
        WHERE project_id IN ({placeholders})
        GROUP BY project_id
        """,  # nosec B608
        tuple(project_ids),
    )
    task_rows = db.fetchall(
        f"""
        SELECT project_id, COUNT(*) AS open_task_count
        FROM tasks
        WHERE project_id IN ({placeholders}) AND closed_at IS NULL
        GROUP BY project_id
        """,  # nosec B608
        tuple(project_ids),
    )

    for row in session_rows:
        stats[row["project_id"]].update(
            session_count=row["session_count"], last_activity_at=row["last_activity_at"]
        )
    for row in task_rows:
        stats[row["project_id"]]["open_task_count"] = row["open_task_count"]
    return stats


def _get_project_stats(server: HTTPServer, project_id: str) -> dict[str, Any]:
    """Get computed stats for one project."""
    return _get_project_stats_batch(server, [project_id])[project_id]


def _project_to_response(
    server: HTTPServer, project: Project, stats: dict[str, Any] | None = None
) -> dict[str, Any]:
    data = cast(dict[str, Any], jsonable_encoder(project.to_dict()))
    data["display_name"] = "Personal" if project.name == "_personal" else project.name
    data.update(stats if stats is not None else _get_project_stats(server, project.id))
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
        projects = await server.run_db(pm.list)

        visible_projects = [
            project for project in projects if project.name not in HIDDEN_PROJECT_NAMES
        ]
        stats_by_project = await server.run_db(
            _get_project_stats_batch, server, [project.id for project in visible_projects]
        )
        results = [
            await server.run_db(_project_to_response, server, project, stats_by_project[project.id])
            for project in visible_projects
        ]

        return results

    @router.get("/{project_id}")
    async def get_project(project_id: str) -> dict[str, Any]:
        """Get a single project with stats."""
        pm = _get_project_manager(server)
        project = await server.run_db(pm.get, project_id)
        if not project or project.deleted_at:
            raise HTTPException(404, "Project not found")

        return cast(dict[str, Any], await server.run_db(_project_to_response, server, project))

    @router.put("/{project_id}")
    async def update_project(project_id: str, body: ProjectUpdate) -> dict[str, Any]:
        """Update project fields."""
        pm = _get_project_manager(server)
        project = await server.run_db(pm.get, project_id)
        if not project or project.deleted_at:
            raise HTTPException(404, "Project not found")

        fields = body.model_dump(exclude_none=True)
        approval_rules = fields.pop("approval_rules", None)
        validation_detection = fields.pop("validation_detection", None)
        original_repo_path = project.repo_path
        requested_repo_path = fields.get("repo_path", original_repo_path)
        repo_path_changed = requested_repo_path != original_repo_path

        if approval_rules is not None and not requested_repo_path:
            raise HTTPException(400, "Project has no repo_path for project-scoped approval rules")

        if validation_detection is not None:
            if not requested_repo_path:
                raise HTTPException(
                    400, "Project has no repo_path for project-scoped validation detection"
                )
            try:
                validation_detection = ValidationDetectionConfig.model_validate(
                    validation_detection
                ).model_dump()
            except ValidationError as exc:
                raise HTTPException(400, str(exc)) from exc

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
                return cast(
                    dict[str, Any],
                    await server.run_db(_project_to_response, server, project),
                )

        def apply_update() -> Project:
            with pm.db.transaction():
                if fields:
                    updated = pm.update(project_id, **fields)
                    if not updated:
                        raise HTTPException(500, "Failed to update project")
                else:
                    updated = project

                if approval_rules is not None:
                    assert updated.repo_path is not None
                    if repo_path_changed and original_repo_path:
                        migrate_project_approval_rules(
                            original_repo_path, updated.repo_path, approval_rules
                        )
                    else:
                        save_project_approval_rules(updated.repo_path, approval_rules)
                elif repo_path_changed and updated.repo_path and migrated_rules:
                    migrate_project_approval_rules(original_repo_path, updated.repo_path)

                if validation_detection is not None:
                    assert updated.repo_path is not None
                    save_project_validation_detection(updated.repo_path, validation_detection)
                elif (
                    repo_path_changed
                    and updated.repo_path
                    and migrated_validation_detection is not None
                ):
                    save_project_validation_detection(
                        updated.repo_path, migrated_validation_detection
                    )

                if (
                    repo_path_changed
                    and original_repo_path
                    and (approval_rules is not None or migrated_rules)
                ):
                    clear_project_approval_rules(original_repo_path)
                if (
                    repo_path_changed
                    and original_repo_path
                    and (
                        validation_detection is not None
                        or migrated_validation_detection is not None
                    )
                ):
                    clear_project_validation_detection(original_repo_path)

                return updated

        updated = await server.run_db(apply_update)

        return cast(dict[str, Any], await server.run_db(_project_to_response, server, updated))

    @router.get("/{project_id}/github-triage")
    async def get_github_triage_config(project_id: str) -> dict[str, Any]:
        """Get GitHub issue triage configuration for a project."""
        pm = _get_project_manager(server)
        project = await server.run_db(pm.get, project_id)
        if not project or project.deleted_at:
            raise HTTPException(404, "Project not found")
        store = GitHubTriageStore(server.services.database)
        config = await server.run_db(
            store.get_config,
            project_id,
            fallback_repo=project.github_repo,
        )
        return cast(dict[str, Any], config.to_dict())

    @router.put("/{project_id}/github-triage")
    async def update_github_triage_config(
        project_id: str,
        body: GitHubTriageConfigUpdate,
    ) -> dict[str, Any]:
        """Update GitHub issue triage configuration for a project."""
        pm = _get_project_manager(server)
        project = await server.run_db(pm.get, project_id)
        if not project or project.deleted_at:
            raise HTTPException(404, "Project not found")

        store = GitHubTriageStore(server.services.database)
        current = await server.run_db(
            store.get_config, project_id, fallback_repo=project.github_repo
        )
        values = body.model_dump(exclude_unset=True)
        interval = values.get(
            "reconcile_interval_seconds",
            current.reconcile_interval_seconds,
        )
        if interval is not None and interval <= 0:
            raise HTTPException(400, "reconcile_interval_seconds must be greater than 0")

        updated = await server.run_db(
            store.upsert_config,
            GitHubTriageConfig(
                project_id=project_id,
                enabled=values.get("enabled", current.enabled),
                webhook_enabled=values.get("webhook_enabled", current.webhook_enabled),
                repositories=tuple(values.get("repositories", current.repositories)),
                reconcile_interval_seconds=interval,
                webhook_secret_ref=values.get("webhook_secret_ref", current.webhook_secret_ref),
            ),
        )
        return cast(dict[str, Any], updated.to_dict())

    @router.delete("/{project_id}")
    async def delete_project(project_id: str) -> dict[str, str]:
        """Soft-delete a project. Protected projects cannot be deleted."""
        pm = _get_project_manager(server)
        project = await server.run_db(pm.get, project_id)
        if not project or project.deleted_at:
            raise HTTPException(404, "Project not found")

        if project.name in SYSTEM_PROJECT_NAMES:
            raise HTTPException(403, f"Cannot delete protected project '{project.name}'")

        if not await server.run_db(pm.soft_delete, project_id):
            raise HTTPException(500, "Failed to delete project")

        return {"status": "deleted", "id": project_id}

    return router
