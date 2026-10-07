"""Tests for the stdio bridge readiness endpoint."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request
from fastapi.routing import APIRoute

from gobby.config.app import DaemonConfig
from gobby.servers.auth_service import _agent_capability_allows
from gobby.servers.routes.mcp.endpoints import request_context
from gobby.servers.routes.mcp.endpoints.bridge import (
    get_bridge_tool_timeouts,
    report_bridge_ready,
)
from gobby.servers.routes.mcp.tools import create_mcp_router
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import SeededContextTokens
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

SESSION_ID = "22222222-2222-4222-8222-222222222222"
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


async def _run_inline(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


def _server(db: HubDatabase) -> Any:
    return SimpleNamespace(session_manager=SimpleNamespace(db=db), run_db=_run_inline)


def test_router_registers_bridge_routes() -> None:
    routes = {
        (route.path, method)
        for route in create_mcp_router().routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }

    assert ("/api/mcp/bridge/ready", "POST") in routes
    assert ("/api/mcp/bridge/tool-timeouts", "GET") in routes


@pytest.mark.asyncio
async def test_bridge_tool_timeouts_serve_the_active_config() -> None:
    config = DaemonConfig.model_validate(
        {"mcp_client_proxy": {"tool_timeouts": {"close_task": 600.0}}}
    )

    result = await get_bridge_tool_timeouts(cast(Any, SimpleNamespace(config=config)))

    assert result == {"success": True, "tool_timeouts": {"close_task": 600.0}}


@pytest.mark.asyncio
async def test_bridge_tool_timeouts_wait_for_daemon_config() -> None:
    with pytest.raises(HTTPException) as raised:
        await get_bridge_tool_timeouts(cast(Any, SimpleNamespace(config=None)))

    assert raised.value.status_code == 503


def test_agent_tokens_may_read_bridge_tool_timeouts() -> None:
    """A spawned agent's bridge reads its timeouts with the run-scoped token."""
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/mcp/bridge/tool-timeouts",
            "headers": [],
        }
    )

    entry = _agent_capability_allows(request)

    assert entry is not None
    assert entry.bind_identity is False


@pytest.mark.asyncio
async def test_bridge_ready_marks_the_resolved_caller_session(
    temp_db: HubDatabase,
    session_storage: SessionManager,
    test_project: dict[str, Any],
) -> None:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        session = session_storage.register(
            external_id="bridge-ready-session",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=test_project["id"],
        )
    tokens = SeededContextTokens(resolved_session_id=session.id)

    with (
        patch.object(
            request_context, "_set_context_for_request", new=AsyncMock(return_value=tokens)
        ),
        patch.object(request_context, "_reset_context") as reset,
    ):
        result = await report_bridge_ready(MagicMock(), _server(temp_db))

    assert result == {"success": True, "session_id": session.id}
    assert SessionVariableManager(temp_db).get_variables(session.id)["_mcp_proxy_ready"] is True
    reset.assert_called_once_with(tokens)


@pytest.mark.asyncio
async def test_bridge_ready_without_a_session_propagates_session_required(
    temp_db: HubDatabase,
) -> None:
    session_required = HTTPException(status_code=409, detail={"error_code": "SESSION_REQUIRED"})

    with patch.object(
        request_context,
        "_set_context_for_request",
        new=AsyncMock(side_effect=session_required),
    ):
        with pytest.raises(HTTPException) as raised:
            await report_bridge_ready(MagicMock(), _server(temp_db))

    assert raised.value.status_code == 409
    assert cast(dict[str, Any], raised.value.detail)["error_code"] == "SESSION_REQUIRED"
    assert "_mcp_proxy_ready" not in SessionVariableManager(temp_db).get_variables(SESSION_ID)
