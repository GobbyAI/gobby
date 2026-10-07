"""Tests for the stdio bridge readiness endpoint."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.mcp_proxy.wait_tools import (
    MCP_WRAPPER_PROTOCOL_VERSION,
    MCP_WRAPPER_PROTOCOL_VERSION_HEADER,
)
from gobby.servers.auth_service import AuthService, _agent_capability_allows
from gobby.servers.routes.mcp.endpoints import request_context
from gobby.servers.routes.mcp.endpoints.bridge import (
    get_bridge_tool_timeouts,
    report_bridge_ready,
)
from gobby.servers.routes.mcp.tools import create_mcp_router
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.auth import AuthStore, hash_token
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.local_token import derive_managed_signing_key, issue_agent_api_token
from gobby.utils.session_context import SeededContextTokens
from gobby.workflows.state_manager import SessionVariableManager
from tests.servers.conftest import _managed_bootstrap, create_http_server

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


def _agent_bridge_client(
    temp_db: HubDatabase, sessions: SessionManager, project_id: str, tmp_path: Path
) -> tuple[TestClient, dict[str, str], str, str]:
    """Serve the app behind a real AuthService; return a managed run's wrapper headers."""
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        own, other = [
            sessions.register(
                external_id=f"bridge-ready-{name}",
                machine_id=LOCAL_MACHINE_ID,
                source="codex",
                project_id=project_id,
            )
            for name in ("own", "other")
        ]
    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=own.id, child_session_id=own.id, provider="codex", prompt="bridge"
    )
    token_file = tmp_path / "operator-token"
    token_file.write_text("bridge-operator-token")
    AuthStore(temp_db).set_local_api_token_hash(hash_token("bridge-operator-token"))
    server = create_http_server(
        database=temp_db,
        session_manager=sessions,
        project_id=project_id,
        authenticated_requests=False,
    )
    server.app.state.server = server
    server.auth_service = AuthService(
        lambda: temp_db, token_file=token_file, bootstrap_file=_managed_bootstrap(token_file)
    )
    token = issue_agent_api_token(
        derive_managed_signing_key("bridge-operator-token"),
        agent_run_id=run.id,
        session_id=own.id,
        project_id=project_id,
    )
    # What DaemonProxy._request sends for a managed run's bridge.
    headers = {
        "Authorization": f"Bearer {token}",
        MCP_WRAPPER_PROTOCOL_VERSION_HEADER: MCP_WRAPPER_PROTOCOL_VERSION,
        "X-Gobby-Project-Id": project_id,
        "X-Gobby-Caller-Project-Id": project_id,
        "X-Gobby-Session-Id": own.id,
        "X-Gobby-Agent-Run-Id": run.id,
    }
    return TestClient(server.app), headers, own.id, other.id


def test_agent_token_marks_its_own_session_ready(
    temp_db: HubDatabase,
    session_storage: SessionManager,
    test_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    client, headers, own_id, _ = _agent_bridge_client(
        temp_db, session_storage, test_project["id"], tmp_path
    )

    response = client.post("/api/mcp/bridge/ready", json={}, headers=headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"success": True, "session_id": own_id}
    assert SessionVariableManager(temp_db).get_variables(own_id)["_mcp_proxy_ready"] is True


@pytest.mark.parametrize("session_header", ["other", "absent"])
def test_agent_token_cannot_mark_another_or_unnamed_session_ready(
    temp_db: HubDatabase,
    session_storage: SessionManager,
    test_project: dict[str, Any],
    tmp_path: Path,
    session_header: str,
) -> None:
    client, headers, own_id, other_id = _agent_bridge_client(
        temp_db, session_storage, test_project["id"], tmp_path
    )
    if session_header == "other":
        headers["X-Gobby-Session-Id"] = other_id
    else:
        del headers["X-Gobby-Session-Id"]

    response = client.post("/api/mcp/bridge/ready", json={}, headers=headers)

    assert response.status_code == 401
    assert response.json()["code"] == "identity_mismatch"
    variables = SessionVariableManager(temp_db)
    assert "_mcp_proxy_ready" not in variables.get_variables(own_id)
    assert "_mcp_proxy_ready" not in variables.get_variables(other_id)
