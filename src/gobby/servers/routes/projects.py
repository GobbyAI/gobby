"""Project management API routes."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from gobby.config.validation_detection import (
    ValidationDetectionConfig,
    load_project_validation_detection,
    save_project_validation_detection,
)
from gobby.servers.tool_approvals import (
    load_project_approval_rules,
    save_project_approval_rules,
)
from gobby.storage.external_issue_sync import ExternalIssueSyncStatusStore
from gobby.storage.github_triage import GitHubTriageConfig, GitHubTriageStore
from gobby.storage.project_checkouts import (
    CheckoutConflictError,
    CheckoutNotFoundError,
    CheckoutRootTakenError,
    CheckoutSentinelRejectedError,
    LocalProjectCheckoutManager,
    MissingMachineContextError,
    OverlayRegistrationRejectedError,
    ProjectCheckout,
    SoftDeletedProjectRejectedError,
    require_root,
)
from gobby.storage.projects import (
    CHECKOUT_FREE_PROJECT_IDS,
    SYSTEM_PROJECT_NAMES,
    IsolatedAgentProjectPathError,
    LocalProjectManager,
    Project,
)
from gobby.storage.workspace_machine_scope import (
    MachineOwnershipMismatchError,
    require_local_machine_id,
)
from gobby.sync.github_issue_sync import (
    GitHubIssueSyncService,
    GitHubRepositoryReadinessError,
)
from gobby.sync.linear import LinearSyncService
from gobby.utils.checkout_root import (
    InvalidCheckoutRootError,
    MarkerMismatchError,
    validate_checkout_root,
)
from gobby.utils.machine_id import get_machine_id

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

HIDDEN_PROJECT_NAMES = frozenset({"_orphaned", "_migrated", "_global"})


class ProjectUpdate(BaseModel):
    """Request body for updating a project."""

    name: str | None = None
    github_url: str | None = None
    github_repo: str | None = None
    linear_team_id: str | None = None
    linear_project_id: str | None = None
    linear_sync_enabled: bool | None = None
    approval_rules: list[str] | None = None
    validation_detection: dict[str, Any] | None = None


class GitHubTriageConfigUpdate(BaseModel):
    """Request body for project GitHub triage config."""

    sync_enabled: bool = False
    triage_enabled: bool = False
    webhook_enabled: bool = False
    repositories: list[str] = Field(default_factory=list)
    reconcile_interval_seconds: int = 3600
    webhook_secret_ref: str | None = None


class CheckoutRootBody(BaseModel):
    """Request body for checkout register and rebind."""

    root_path: str


_CHECKOUT_HTTP_CONFLICTS = (
    MissingMachineContextError,
    MachineOwnershipMismatchError,
    CheckoutConflictError,
    CheckoutRootTakenError,
    OverlayRegistrationRejectedError,
    MarkerMismatchError,
    CheckoutSentinelRejectedError,
    SoftDeletedProjectRejectedError,
    CheckoutNotFoundError,
    IsolatedAgentProjectPathError,
)


def _checkout_payload(checkout: ProjectCheckout | None) -> dict[str, str] | None:
    if checkout is None:
        return None
    return {"machine_id": checkout.machine_id, "root_path": checkout.root_path}


def _checkout_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, InvalidCheckoutRootError):
        return HTTPException(400, detail={"error": type(exc).__name__, "message": str(exc)})
    if isinstance(exc, _CHECKOUT_HTTP_CONFLICTS):
        return HTTPException(409, detail={"error": type(exc).__name__, "message": str(exc)})
    raise exc


def _require_checkout_machine(project_id: str, provided_machine_id: str | None) -> str:
    try:
        return require_local_machine_id(
            provided_machine_id,
            resource_kind="project_checkout",
            resource_id=project_id,
        )
    except RuntimeError as exc:
        if isinstance(exc, MachineOwnershipMismatchError):
            raise
        raise MissingMachineContextError(str(exc)) from exc


def _reject_checkout_sentinel(project_id: str) -> None:
    if project_id in CHECKOUT_FREE_PROJECT_IDS:
        raise CheckoutSentinelRejectedError(
            f"checkout-free sentinel project {project_id} cannot own a checkout"
        )


def _local_checkout(db: Any, project_id: str) -> ProjectCheckout | None:
    machine_id = get_machine_id()
    if not machine_id:
        return None
    return LocalProjectCheckoutManager(db).get(machine_id, project_id)


def _local_checkouts(db: Any, project_ids: Iterable[str]) -> dict[str, ProjectCheckout]:
    """Return this daemon's checkouts for `project_ids`, keyed by project id, in one query."""
    machine_id = get_machine_id()
    wanted = set(project_ids)
    if not machine_id or not wanted:
        return {}
    return {
        checkout.project_id: checkout
        for checkout in LocalProjectCheckoutManager(db).list_for_machine(machine_id)
        if checkout.project_id in wanted
    }


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


def _build_project_response(
    project: Project, stats: dict[str, Any], checkout: ProjectCheckout | None
) -> dict[str, Any]:
    """Shape one project payload from already-resolved stats and checkout."""
    data = cast(dict[str, Any], jsonable_encoder(project.to_dict()))
    data["display_name"] = "Personal" if project.name == "_personal" else project.name
    data.update(stats)
    data["checkout"] = _checkout_payload(checkout)
    if checkout is None:
        data["approval_rules"] = []
        data["validation_detection"] = None
    else:
        data["approval_rules"] = load_project_approval_rules(checkout.root_path)
        data["validation_detection"] = load_project_validation_detection(checkout.root_path)
    return data


def _project_to_response(
    server: HTTPServer, project: Project, stats: dict[str, Any] | None = None
) -> dict[str, Any]:
    if stats is None:
        stats = _get_project_stats(server, project.id)
    checkout = _local_checkout(_get_project_manager(server).db, project.id)
    return _build_project_response(project, stats, checkout)


def _projects_to_responses(server: HTTPServer, projects: Sequence[Project]) -> list[dict[str, Any]]:
    """Shape list payloads with one stats batch and one checkout query per request."""
    if not projects:
        return []
    db = _get_project_manager(server).db
    project_ids = [project.id for project in projects]
    stats_by_project = _get_project_stats_batch(server, project_ids)
    checkouts = _local_checkouts(db, project_ids)
    return [
        _build_project_response(project, stats_by_project[project.id], checkouts.get(project.id))
        for project in projects
    ]


def create_projects_router(server: HTTPServer) -> APIRouter:
    """Create the projects API router."""
    router = APIRouter(prefix="/api/projects", tags=["projects"])

    async def _broadcast_project(event: str, project_id: str, **kwargs: Any) -> None:
        """Broadcast a project event via WebSocket if available."""
        ws = server.services.websocket_server
        if ws:
            try:
                await ws.broadcast_project_event(event, project_id, **kwargs)
            except Exception as e:
                logger.warning(
                    "Failed to broadcast project event '%s' for project %s: %s",
                    event,
                    project_id,
                    e,
                )

    @router.get("")
    async def list_projects() -> list[dict[str, Any]]:
        """List all projects with computed stats."""
        pm = _get_project_manager(server)
        projects = await server.run_db(pm.list)

        visible_projects = [
            project for project in projects if project.name not in HIDDEN_PROJECT_NAMES
        ]
        results: list[dict[str, Any]] = await server.run_db(
            _projects_to_responses, server, visible_projects
        )
        return results

    @router.get("/{project_id}")
    async def get_project(project_id: str) -> dict[str, Any]:
        """Get a single project with stats."""
        pm = _get_project_manager(server)
        project = await server.run_db(pm.get, project_id)
        if not project or project.deleted_at:
            raise HTTPException(404, "Project not found")

        return cast(dict[str, Any], await server.run_db(_project_to_response, server, project))

    @router.get("/{project_id}/checkouts")
    async def get_checkout(project_id: str) -> dict[str, Any]:
        """Return the calling daemon's checkout object-or-null."""
        pm = _get_project_manager(server)
        project = await server.run_db(pm.get, project_id)
        if not project or project.deleted_at:
            raise HTTPException(404, "Project not found")
        checkout = await server.run_db(_local_checkout, pm.db, project_id)
        return {"checkout": _checkout_payload(checkout)}

    @router.post("/{project_id}/checkouts")
    async def register_checkout(project_id: str, body: CheckoutRootBody) -> JSONResponse:
        """Register this daemon's checkout. 201 on insert, 200 on same-root retry."""
        pm = _get_project_manager(server)
        project = await server.run_db(pm.get, project_id)
        if not project:
            raise HTTPException(404, "Project not found")
        if project.deleted_at is not None:
            raise _checkout_http_error(
                SoftDeletedProjectRejectedError(
                    f"project {project_id} is soft-deleted; HTTP register does not restore"
                )
            )

        def apply_register() -> tuple[ProjectCheckout, bool]:
            try:
                _reject_checkout_sentinel(project_id)
                machine_id = _require_checkout_machine(project_id, None)
                root = validate_checkout_root(
                    pm.db,
                    project_id=project_id,
                    machine_id=machine_id,
                    candidate_path=body.root_path,
                    expected_marker_id=project_id,
                )
                return LocalProjectCheckoutManager(pm.db).register(machine_id, project_id, root)
            except (
                *_CHECKOUT_HTTP_CONFLICTS,
                InvalidCheckoutRootError,
            ) as exc:
                raise _checkout_http_error(exc) from exc

        checkout, created = await server.run_db(apply_register)
        payload = _checkout_payload(checkout)
        if created:
            await _broadcast_project("checkout_registered", project_id, checkout=payload)
        return JSONResponse(
            status_code=201 if created else 200,
            content={"checkout": payload},
        )

    @router.post("/{project_id}/checkouts/{machine_id}/rebind")
    async def rebind_checkout(
        project_id: str, machine_id: str, body: CheckoutRootBody
    ) -> dict[str, Any]:
        """Rebind this daemon's checkout. Soft-deleted projects stay deleted."""
        pm = _get_project_manager(server)
        project = await server.run_db(pm.get, project_id)
        if not project:
            raise HTTPException(404, "Project not found")

        def apply_rebind() -> ProjectCheckout:
            try:
                _reject_checkout_sentinel(project_id)
                local_machine_id = _require_checkout_machine(project_id, machine_id)
                root = validate_checkout_root(
                    pm.db,
                    project_id=project_id,
                    machine_id=local_machine_id,
                    candidate_path=body.root_path,
                    expected_marker_id=project_id,
                )
                return LocalProjectCheckoutManager(pm.db).rebind(local_machine_id, project_id, root)
            except (
                *_CHECKOUT_HTTP_CONFLICTS,
                InvalidCheckoutRootError,
            ) as exc:
                raise _checkout_http_error(exc) from exc

        checkout = await server.run_db(apply_rebind)
        payload = _checkout_payload(checkout)
        await _broadcast_project("checkout_rebound", project_id, checkout=payload)
        return {"checkout": payload}

    @router.put("/{project_id}")
    @router.patch("/{project_id}")
    async def update_project(project_id: str, body: ProjectUpdate) -> dict[str, Any]:
        """Update project fields."""
        pm = _get_project_manager(server)
        project = await server.run_db(pm.get, project_id)
        if not project or project.deleted_at:
            raise HTTPException(404, "Project not found")

        fields = body.model_dump(exclude_unset=True)
        approval_rules = fields.pop("approval_rules", None)
        validation_detection = fields.pop("validation_detection", None)

        effective_linear_sync_enabled = fields.get(
            "linear_sync_enabled", project.linear_sync_enabled
        )
        if effective_linear_sync_enabled:
            team_id = fields.get("linear_team_id", project.linear_team_id)
            linear_project_id = fields.get("linear_project_id", project.linear_project_id)
            if not team_id or not linear_project_id:
                raise HTTPException(
                    400,
                    "Linear sync requires both linear_team_id and linear_project_id",
                )

        if validation_detection is not None:
            try:
                validation_detection = ValidationDetectionConfig.model_validate(
                    validation_detection
                ).model_dump()
            except ValidationError as exc:
                raise HTTPException(400, str(exc)) from exc

        checkout_root: str | None = None
        if approval_rules is not None or validation_detection is not None:
            try:
                checkout_root = require_root(
                    pm.db,
                    project_id,
                    _require_checkout_machine(project_id, None),
                )
            except (
                *_CHECKOUT_HTTP_CONFLICTS,
                InvalidCheckoutRootError,
            ) as exc:
                raise _checkout_http_error(exc) from exc

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
                    assert checkout_root is not None
                    save_project_approval_rules(checkout_root, approval_rules)

                if validation_detection is not None:
                    assert checkout_root is not None
                    save_project_validation_detection(checkout_root, validation_detection)

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
        interval = values.get("reconcile_interval_seconds")
        if interval is None:
            interval = current.reconcile_interval_seconds
        if interval is not None and interval <= 0:
            raise HTTPException(400, "reconcile_interval_seconds must be greater than 0")

        candidate = GitHubTriageConfig(
            project_id=project_id,
            sync_enabled=(
                current.sync_enabled
                if values.get("sync_enabled") is None
                else values["sync_enabled"]
            ),
            triage_enabled=(
                current.triage_enabled
                if values.get("triage_enabled") is None
                else values["triage_enabled"]
            ),
            webhook_enabled=(
                current.webhook_enabled
                if values.get("webhook_enabled") is None
                else values["webhook_enabled"]
            ),
            repositories=tuple(
                current.repositories
                if values.get("repositories") is None
                else values["repositories"]
            ),
            reconcile_interval_seconds=interval,
            webhook_secret_ref=(
                values["webhook_secret_ref"]
                if "webhook_secret_ref" in values
                else current.webhook_secret_ref
            ),
        )
        if candidate.sync_enabled or candidate.triage_enabled:
            if server.services.mcp_manager is None:
                raise HTTPException(400, "GitHub connector is unavailable")
            readiness = GitHubIssueSyncService(
                db=server.services.database,
                mcp_manager=server.services.mcp_manager,
                task_manager=server.services.task_manager,
                project_manager=pm,
            )
            try:
                await readiness.check_access(project, candidate)
            except GitHubRepositoryReadinessError as exc:
                raise HTTPException(400, str(exc)) from exc

        updated = await server.run_db(store.upsert_config, candidate)
        return cast(dict[str, Any], updated.to_dict())

    @router.get("/{project_id}/integrations/status")
    async def get_integrations_status(project_id: str) -> dict[str, Any]:
        """Return configuration, readiness, and reconciliation health."""
        pm = _get_project_manager(server)
        project = await server.run_db(pm.get, project_id)
        if not project or project.deleted_at:
            raise HTTPException(404, "Project not found")

        status_store = ExternalIssueSyncStatusStore(server.services.database)
        linear_status = await server.run_db(status_store.get, project_id, "linear")
        github_status = await server.run_db(status_store.get, project_id, "github")
        linear_counts = await server.run_db(status_store.counts, project_id, "linear")
        github_counts = await server.run_db(status_store.counts, project_id, "github")
        github_store = GitHubTriageStore(server.services.database)
        github_config = await server.run_db(
            github_store.get_config,
            project_id,
            fallback_repo=project.github_repo,
        )

        linear_service = None
        if server.services.mcp_manager is not None:
            linear_service = LinearSyncService(
                mcp_manager=server.services.mcp_manager,
                task_manager=server.services.task_manager,
                project_id=project_id,
                linear_team_id=project.linear_team_id,
                linear_project_id=project.linear_project_id,
                project_manager=pm,
            )
        linear_ready = bool(
            project.linear_team_id
            and project.linear_project_id
            and linear_service
            and linear_service.is_available()
        )
        linear_error = None
        if not linear_ready:
            linear_error = (
                "Linear team and project binding are required"
                if not project.linear_team_id or not project.linear_project_id
                else (
                    linear_service.get_unavailable_reason()
                    if linear_service
                    else "Linear connector is unavailable"
                )
            )

        github_ready = False
        github_error = None
        repositories: tuple[str, ...] = ()
        if server.services.mcp_manager is None:
            github_error = "GitHub connector is unavailable"
        else:
            github_service = GitHubIssueSyncService(
                db=server.services.database,
                mcp_manager=server.services.mcp_manager,
                task_manager=server.services.task_manager,
                project_manager=pm,
            )
            try:
                repositories = github_service.repositories_for(project, github_config)
            except ValueError:
                repositories = ()
            try:
                repositories = await github_service.check_access(project, github_config)
                github_ready = True
            except GitHubRepositoryReadinessError as exc:
                github_error = str(exc)

        def status_payload(status: Any, counts: tuple[int, int]) -> dict[str, Any]:
            if status:
                payload = cast(dict[str, Any], status.to_dict())
                payload.pop("project_id", None)
                payload.pop("provider", None)
                payload["linked_count"] = counts[0]
                payload["pending_count"] = counts[1]
                return payload
            return {
                "state": "pending",
                "linked_count": counts[0],
                "pending_count": counts[1],
                "consecutive_failures": 0,
                "last_attempt_at": None,
                "last_success_at": None,
                "last_outbound_success_at": None,
                "retry_at": None,
                "last_statistics": {},
                "last_error": None,
            }

        github_config_payload = github_config.to_dict()
        github_config_payload.pop("project_id", None)

        return cast(
            dict[str, Any],
            jsonable_encoder(
                {
                    "project_id": project_id,
                    "linear": {
                        "enabled": project.linear_sync_enabled,
                        "ready": linear_ready,
                        "binding": {
                            "team_id": project.linear_team_id,
                            "project_id": project.linear_project_id,
                        },
                        "readiness_error": linear_error,
                        **status_payload(linear_status, linear_counts),
                    },
                    "github": {
                        **github_config_payload,
                        "ready": github_ready,
                        "repositories": list(repositories or github_config.repositories),
                        "readiness_error": github_error,
                        **status_payload(github_status, github_counts),
                    },
                }
            ),
        )

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

    @router.post("/{project_id}/purge")
    async def purge_project(project_id: str) -> dict[str, Any]:
        """Run the shared lifecycle-safe hard purge service immediately."""
        runner = server.get_runner()
        service = getattr(runner, "project_purge_service", None)
        if service is None:
            raise HTTPException(503, "Project purge service is unavailable")
        outcome = await service.purge_project(project_id)
        status_codes = {"not_found": 404, "protected": 403, "failed": 500}
        if not outcome.success:
            raise HTTPException(
                status_codes.get(outcome.status, 500),
                {
                    "project_id": outcome.project_id,
                    "status": outcome.status,
                    "message": outcome.message,
                },
            )
        return {
            "project_id": outcome.project_id,
            "status": outcome.status,
            "message": outcome.message,
        }

    return router
