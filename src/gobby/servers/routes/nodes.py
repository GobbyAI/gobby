"""Node key-bound routes: checkout registration and run reports.

Both routes take the machine from the front door's verified key identity, never
from the body, and never touch the reported paths on the hub's disk.
"""

from __future__ import annotations

import json
import logging
import os
from functools import cache
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from gobby.config.node_reports import NodeReportsConfig
from gobby.hub.node_commands import (
    CONTRACT_VERSION,
    CheckoutBody,
    CheckoutResult,
    ReportBody,
    StaleReport,
)
from gobby.hub.node_reports import (
    Admission,
    node_report_inbox,
    record_remote_workspaces,
    report_size,
)
from gobby.hub.remote_pane import apply_report_panes, redact_report_panes
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

logger = logging.getLogger(__name__)

# The node reports every 30 seconds, so a refused report waits one interval.
REPORT_RETRY_AFTER_SECONDS = 30


def _node_machine(server: HTTPServer, request: Request) -> str:
    identity = server.auth_service.verified_front_door_identity(request)
    if identity is None:
        raise HTTPException(403, "node_identity_required")
    if is_local_machine_owner(identity.machine_id, require_machine_id()):
        raise HTTPException(403, "machine_is_local")
    return identity.machine_id


def _report_budgets(server: HTTPServer) -> NodeReportsConfig:
    config = server.config
    return config.node_reports if config is not None else NodeReportsConfig()


async def _capped_body(request: Request, cap: int, code: str) -> bytes:
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > cap:
            raise HTTPException(413, code)
    return bytes(raw)


def _body[T: BaseModel](raw: bytes, model: type[T]) -> T:
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(422, "invalid_body") from exc
    if not isinstance(body, dict) or type(body.get("v")) is not int:
        raise HTTPException(422, "invalid_body")
    if body["v"] != CONTRACT_VERSION:
        raise HTTPException(422, "unsupported_version")
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        _log_invalid_body(model, exc)
        raise HTTPException(422, "invalid_body") from exc


def _log_invalid_body(model: type[BaseModel], exc: ValidationError) -> None:
    # A pydantic error quotes the input and the request's keys, so the log keeps only the
    # schema's field names, [index] for list positions, [key] for anything else, and the type.
    names = _schema_field_names(model)
    for error in exc.errors():
        location = ".".join(_loc_segment(segment, names) for segment in error["loc"])
        logger.warning("Node body rejected at %s: %s", location, error["type"])


def _loc_segment(segment: int | str, names: frozenset[str]) -> str:
    if isinstance(segment, int):
        return "[index]"
    return segment if segment in names else "[key]"


@cache
def _schema_field_names(model: type[BaseModel]) -> frozenset[str]:
    names: set[str] = set()
    _collect_property_names(model.model_json_schema(), names)
    return frozenset(names)


def _collect_property_names(node: object, names: set[str]) -> None:
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
        for value in node.values():
            _collect_property_names(value, names)
    elif isinstance(node, list):
        for item in node:
            _collect_property_names(item, names)


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


async def _apply_report_panes(server: HTTPServer, machine_id: str, body: ReportBody) -> None:
    monitor = server.services.agent_lifecycle_monitor
    if monitor is None:
        logger.warning("Node pane state not applied: no agent lifecycle monitor")
        return
    try:
        await apply_report_panes(
            server.services.database,
            monitor.attention_tracker,
            machine_id=machine_id,
            revision=body.revision,
            agents=body.agents,
            run_db=server.run_db,
        )
    except Exception as exc:
        logger.warning("Node pane state not applied: %s", type(exc).__name__)


def create_nodes_router(server: HTTPServer) -> APIRouter:
    router = APIRouter(prefix="/api/nodes", tags=["nodes"])

    @router.post("/checkouts")
    async def register_checkout(request: Request) -> dict[str, Any]:
        machine_id = _node_machine(server, request)
        budgets = _report_budgets(server)
        body = _body(
            await _capped_body(request, budgets.max_request_bytes, "checkout_too_large"),
            CheckoutBody,
        )
        result: CheckoutResult = await server.run_db(_register_checkout, server, machine_id, body)
        return result.model_dump(mode="json")

    @router.post("/report", response_model=None)
    async def report(request: Request) -> dict[str, Any] | JSONResponse:
        machine_id = _node_machine(server, request)
        budgets = _report_budgets(server)
        body = redact_report_panes(
            _body(
                await _capped_body(request, budgets.max_request_bytes, "report_too_large"),
                ReportBody,
            )
        )
        inbox = node_report_inbox()
        size = report_size(body)
        admission = inbox.admit(
            machine_id,
            body.hub_epoch,
            body.revision,
            size,
            budgets.max_pending_bytes_per_machine,
        )
        if admission is Admission.STALE:
            stale = StaleReport(code="stale_report", hub_epoch=inbox.hub_epoch)
            return JSONResponse(stale.model_dump(mode="json"), status_code=409)
        if admission is Admission.BACKLOG_FULL:
            raise HTTPException(
                429,
                "report_backlog_full",
                headers={"Retry-After": str(REPORT_RETRY_AFTER_SECONDS)},
            )
        try:
            await server.run_db(
                record_remote_workspaces, server.services.database, machine_id, body
            )
        except BaseException:
            inbox.release(machine_id, size)
            raise
        inbox.put(machine_id, body, size)
        await _apply_report_panes(server, machine_id, body)
        return {}

    return router
