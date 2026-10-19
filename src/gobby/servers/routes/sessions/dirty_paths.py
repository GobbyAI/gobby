"""Operator route that records an approval for a session's unowned dirty paths.

Only a request whose front-door identity gdaemon verified from an operator API key
is admitted. Agent bearers, managed claims, cookies and unauthenticated requests
are refused with 403 before the session is touched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from gobby.storage.session_resolution import resolve_session_reference
from gobby.workflows.dirty_edit_grants import record_dirty_path_approval

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


class DirtyPathApprovalRequest(BaseModel):
    checkout_root: str
    paths: list[str]


def register_dirty_path_routes(router: APIRouter, server: HTTPServer) -> None:
    @router.post("/{session_ref}/dirty-path-approvals")
    async def approve_dirty_paths(
        session_ref: str, payload: DirtyPathApprovalRequest, request: Request
    ) -> dict[str, Any]:
        identity = server.auth_service.verified_front_door_identity(request)
        if identity is None:
            raise HTTPException(status_code=403, detail="Operator API key required")
        if server.session_manager is None:
            raise HTTPException(status_code=503, detail="Session manager not available")
        db = server.session_manager.db
        try:
            session_id = await server.run_db(resolve_session_reference, db, session_ref)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            approvals = await server.run_db(
                record_dirty_path_approval,
                db,
                session_id,
                checkout_root=payload.checkout_root,
                paths=payload.paths,
                approved_by=f"{identity.user_id}/{identity.key_id}",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"session_id": session_id, "approvals": approvals}
