"""Node transport adapters for hub-owned hook replay and receipt decisions."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gobby.hooks.replay_decision import acknowledge_and_apply_receipt, decide_replay
from gobby.sessions.machine_scope import is_local_machine_owner
from gobby.storage.hook_receipts import release_receipt
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    v: Literal[1]
    envelope_id: str
    envelope: dict[str, Any]
    age_seconds: float = Field(ge=0, allow_inf_nan=False)
    processed: bool
    live: bool


class ReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    v: Literal[1]
    op: Literal["ack", "release"]
    receipt_id: str
    delivery_generation: int = Field(ge=1)


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
    if isinstance(body, dict) and type(body.get("v")) is int and body["v"] != 1:
        raise HTTPException(422, "unsupported_version")
    if not isinstance(body, dict) or type(body.get("v")) is not int:
        raise HTTPException(422, "invalid_body")
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(422, "invalid_body") from exc


def _receipt(
    server: HTTPServer, request: Request, body: ReceiptRequest, machine: str
) -> dict[str, Any]:
    db = server.services.database
    refused = (
        {"acknowledged": False, "carrying_envelope_ids": []}
        if body.op == "ack"
        else {"released": False}
    )
    row = db.fetchone(
        "SELECT s.machine_id FROM hook_receipt_effects r "
        "JOIN sessions s ON s.id = r.session_id WHERE r.receipt_id::text = %s",
        (body.receipt_id,),
    )
    if row is None or not is_local_machine_owner(str(row["machine_id"]), machine):
        return refused
    if body.op == "release":
        return {
            "released": release_receipt(
                db, receipt_id=body.receipt_id, delivery_generation=body.delivery_generation
            )
            is not None
        }
    manager = getattr(request.app.state, "hook_manager", None)
    committed = acknowledge_and_apply_receipt(
        db,
        receipt_id=body.receipt_id,
        delivery_generation=body.delivery_generation,
        message_manager=getattr(manager, "_inter_session_msg_manager", None),
        variable_manager=SessionVariableManager(db),
    )
    if committed is None:
        return refused
    return {
        "acknowledged": True,
        "carrying_envelope_ids": list(
            dict.fromkeys(
                [
                    committed.original_envelope_id,
                    committed.current_envelope_id,
                ]
            )
        ),
    }


def create_node_hooks_router(server: HTTPServer) -> APIRouter:
    router = APIRouter(prefix="/api/nodes", tags=["nodes"])

    @router.post("/hooks/replay")
    async def replay(request: Request) -> dict[str, Any]:
        machine = _node_machine(server, request)
        body = await _body(request, ReplayRequest)
        decision = await asyncio.to_thread(
            decide_replay,
            request.app,
            body.envelope_id,
            body.envelope,
            age_seconds=body.age_seconds,
            processed=body.processed,
            live=body.live,
            machine_id=machine,
        )
        return asdict(decision)

    @router.post("/receipts")
    async def receipts(request: Request) -> dict[str, Any]:
        machine = _node_machine(server, request)
        body = await _body(request, ReceiptRequest)
        return await asyncio.to_thread(_receipt, server, request, body, machine)

    return router
