"""Session-scoped runtime variable get/set routes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


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
    async def set_variable(session_id: str, request: SetVariableRequest) -> dict[str, Any]:
        if server.session_manager is None:
            raise HTTPException(status_code=503, detail="Session manager not available")
        try:
            from gobby.mcp_proxy.tools.workflows._variables import set_variable as _set_var
            from gobby.workflows.step_instances import AgentStepInstanceManager

            instance_manager = (
                AgentStepInstanceManager(server.session_manager.db)
                if request.scope == "step"
                else None
            )
            return _set_var(
                server.session_manager,
                server.session_manager.db,
                name=request.name,
                value=request.value,
                session_id=session_id,
                scope=request.scope,
                instance_manager=instance_manager,
            )
        except Exception as e:
            logger.exception("Error setting variable: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/{session_id}/variables/get")
    async def get_variable(session_id: str, request: GetVariableRequest) -> dict[str, Any]:
        if server.session_manager is None:
            raise HTTPException(status_code=503, detail="Session manager not available")
        try:
            from gobby.mcp_proxy.tools.workflows._variables import get_variable as _get_var
            from gobby.workflows.step_instances import AgentStepInstanceManager

            instance_manager = (
                AgentStepInstanceManager(server.session_manager.db)
                if request.scope == "step"
                else None
            )
            return _get_var(
                server.session_manager,
                server.session_manager.db,
                name=request.name,
                session_id=session_id,
                scope=request.scope,
                instance_manager=instance_manager,
            )
        except Exception as e:
            logger.exception("Error getting variable: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e
