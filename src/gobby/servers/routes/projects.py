"""Project management API routes."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from gobby.config.validation_detection import (
    ValidationDetectionConfig,
    load_project_validation_detection,
    save_project_validation_detection,
)
from gobby.servers.tool_approvals import (
    load_project_approval_rules,
    save_project_approval_rules,
)
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
    NameAttachRejectedError,
    Project,
)
from gobby.storage.sessions._constants import LIVE_SESSION_STATUS_ORDER
from gobby.storage.workspace_machine_scope import (
    MachineOwnershipMismatchError,
    require_local_machine_id,
)
from gobby.utils.checkout_root import (
    InvalidCheckoutRootError,
    MarkerMismatchError,
    validate_checkout_root,
)
from gobby.utils.machine_id import get_machine_id
from gobby.utils.project_init import initialize_project

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

HIDDEN_PROJECT_NAMES = frozenset({"_orphaned", "_migrated", "_global"})


class ProjectUpdate(BaseModel):
    """Request body for updating a project."""

    name: str | None = None
    approval_rules: list[str] | None = None
    validation_detection: dict[str, Any] | None = None


class CheckoutRootBody(BaseModel):
    """Request body for checkout register and rebind."""

    root_path: str


class ProjectInitBody(BaseModel):
    """Request body for project initialization."""

    path: str


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
    NameAttachRejectedError,
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
               COUNT(*) FILTER (
                   WHERE status = ANY(%s)
               ) AS session_count,
               MAX(updated_at) AS last_activity_at
        FROM sessions
        WHERE project_id IN ({placeholders})
        GROUP BY project_id
        """,  # nosec B608
        (list(LIVE_SESSION_STATUS_ORDER), *project_ids),
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

    @router.post("/init")
    async def init_project(body: ProjectInitBody) -> dict[str, Any]:
        """Initialize a local directory and return its project payload."""
        pm = _get_project_manager(server)
        project_path = Path(body.path)

        def apply_init() -> tuple[Project, bool]:
            try:
                machine_id = _require_checkout_machine(body.path, None)
                existing_project_ids = {
                    checkout.project_id
                    for checkout in LocalProjectCheckoutManager(pm.db).list_for_machine(machine_id)
                }
                result = initialize_project(
                    cwd=project_path,
                    db=pm.db,
                )
                project = pm.get(result.project_id)
                if project is None:
                    raise RuntimeError(f"Project {result.project_id} not found after init")
                return project, result.project_id not in existing_project_ids
            except (
                *_CHECKOUT_HTTP_CONFLICTS,
                InvalidCheckoutRootError,
            ) as exc:
                raise _checkout_http_error(exc) from exc

        project, checkout_created = await server.run_db(apply_init)
        payload = cast(
            dict[str, Any],
            await server.run_db(_project_to_response, server, project),
        )
        if checkout_created:
            await _broadcast_project(
                "checkout_registered",
                project.id,
                checkout=payload["checkout"],
            )
        return payload

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
