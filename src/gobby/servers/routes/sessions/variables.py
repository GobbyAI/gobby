"""Session-scoped runtime variable get/set routes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from gobby.storage.session_resolution import resolve_session_reference

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def _bound_session_id(server: HTTPServer, request: Request, session_id: str) -> str:
    """Agent tokens may only read/write their claimed session."""
    claims = server.auth_service.verified_agent_claims(request)
    if claims is None:
        return session_id
    if session_id == claims.session_id:
        return claims.session_id
    if server.session_manager is None:
        raise HTTPException(status_code=503, detail="Session manager not available")
    try:
        resolved = resolve_session_reference(
            server.session_manager.db,
            session_id,
            claims.project_id,
        )
    except Exception:
        resolved = None
    if resolved != claims.session_id:
        raise HTTPException(status_code=403, detail="Session does not match agent capability")
    return claims.session_id


class SetVariableRequest(BaseModel):
    """Request body for setting a session or step variable."""

    name: str
    value: Any = None
    scope: Literal["session", "step"] = "session"


class GetVariableRequest(BaseModel):
    """Request body for getting session or step variable(s)."""

    name: str | None = None
    scope: Literal["session", "step"] = "session"


def register_session_variable_routes(router: APIRouter, server: HTTPServer) -> None:
    """Register POST /{session_id}/variables/get|set on the sessions router."""

    @router.post("/{session_id}/variables/set")
    async def set_variable(
        session_id: str, payload: SetVariableRequest, request: Request
    ) -> dict[str, Any]:
        if server.session_manager is None:
            raise HTTPException(status_code=503, detail="Session manager not available")
        bound_session_id = _bound_session_id(server, request, session_id)
        try:
            from gobby.mcp_proxy.tools.workflows._variables import set_variable as _set_var
            from gobby.workflows.step_instances import AgentStepInstanceManager

            instance_manager = (
                AgentStepInstanceManager(server.session_manager.db)
                if payload.scope == "step"
                else None
            )
            return _set_var(
                server.session_manager,
                server.session_manager.db,
                name=payload.name,
                value=payload.value,
                session_id=bound_session_id,
                scope=payload.scope,
                instance_manager=instance_manager,
            )
        except Exception as e:
            logger.exception("Error setting variable: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/{session_id}/variables/get")
    async def get_variable(
        session_id: str, payload: GetVariableRequest, request: Request
    ) -> dict[str, Any]:
        if server.session_manager is None:
            raise HTTPException(status_code=503, detail="Session manager not available")
        bound_session_id = _bound_session_id(server, request, session_id)
        try:
            from gobby.mcp_proxy.tools.workflows._variables import get_variable as _get_var
            from gobby.workflows.step_instances import AgentStepInstanceManager

            instance_manager = (
                AgentStepInstanceManager(server.session_manager.db)
                if payload.scope == "step"
                else None
            )
            return _get_var(
                server.session_manager,
                server.session_manager.db,
                name=payload.name,
                session_id=bound_session_id,
                scope=payload.scope,
                instance_manager=instance_manager,
            )
        except Exception as e:
            logger.exception("Error getting variable: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e
