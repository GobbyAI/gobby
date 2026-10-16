"""Node key-bound routes: checkout registration and run reports.

Both routes take the machine from the front door's verified key identity, never
from the body, and never touch the reported paths on the hub's disk.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from gobby.hub.node_commands import (
    CONTRACT_VERSION,
    CheckoutBody,
    CheckoutResult,
    ReportBody,
    StaleReport,
)
from gobby.hub.node_reports import node_report_inbox, record_remote_workspaces
from gobby.sessions.machine_scope import is_local_machine_owner
from gobby.storage.project_checkouts import (
    CheckoutConflictError,
    CheckoutRootTakenError,
    CheckoutSentinelRejectedError,
    LocalProjectCheckoutManager,
    OverlayRegistrationRejectedError,
    SoftDeletedProjectRejectedError,
)
from gobby.storage.projects import LocalProjectManager
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


def _node_machine(server: HTTPServer, request: Request) -> str:
    identity = server.auth_service.verified_front_door_identity(request)
    if identity is None:
        raise HTTPException(403, "node_identity_required")
    if is_local_machine_owner(identity.machine_id, require_machine_id()):
        raise HTTPException(403, "machine_is_local")
    return identity.machine_id


async def _body[T: BaseModel](request: Request, model: type[T]) -> T:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(422, "invalid_body") from exc
    if not isinstance(body, dict) or type(body.get("v")) is not int:
        raise HTTPException(422, "invalid_body")
    if body["v"] != CONTRACT_VERSION:
        raise HTTPException(422, "unsupported_version")
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(422, "invalid_body") from exc


def _register_checkout(server: HTTPServer, machine_id: str, body: CheckoutBody) -> CheckoutResult:
    db = server.services.database
    project = LocalProjectManager(db).get(body.project_id)
    if project is None:
        raise HTTPException(404, "project_not_found")
    if project.deleted_at is not None:
        raise HTTPException(409, "project_deleted")
    if not os.path.isabs(body.root_path):
        raise HTTPException(422, "invalid_body")
    try:
        checkout, created = LocalProjectCheckoutManager(db).register(
            machine_id, project.id, body.root_path
        )
    except CheckoutConflictError as exc:
        raise HTTPException(409, "checkout_conflict") from exc
    except CheckoutRootTakenError as exc:
        raise HTTPException(409, "checkout_root_taken") from exc
    except SoftDeletedProjectRejectedError as exc:
        raise HTTPException(409, "project_deleted") from exc
    except (CheckoutSentinelRejectedError, OverlayRegistrationRejectedError) as exc:
        raise HTTPException(422, "checkout_rejected") from exc
    return CheckoutResult(root_path=checkout.root_path, created=created)


def create_nodes_router(server: HTTPServer) -> APIRouter:
    router = APIRouter(prefix="/api/nodes", tags=["nodes"])

    @router.post("/checkouts")
    async def register_checkout(request: Request) -> dict[str, Any]:
        machine_id = _node_machine(server, request)
        body = await _body(request, CheckoutBody)
        result: CheckoutResult = await server.run_db(_register_checkout, server, machine_id, body)
        return result.model_dump(mode="json")

    @router.post("/report", response_model=None)
    async def report(request: Request) -> dict[str, Any] | JSONResponse:
        machine_id = _node_machine(server, request)
        body = await _body(request, ReportBody)
        inbox = node_report_inbox()
        if not inbox.admit(machine_id, body.hub_epoch, body.revision):
            stale = StaleReport(code="stale_report", hub_epoch=inbox.hub_epoch)
            return JSONResponse(stale.model_dump(mode="json"), status_code=409)
        await server.run_db(record_remote_workspaces, server.services.database, machine_id, body)
        inbox.put(machine_id, body)
        return {}

    return router
