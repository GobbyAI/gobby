"""HTTP agent-run identity checks after automatic close review retirement."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from gobby.servers.routes.dependencies import get_server
from gobby.servers.routes.mcp.endpoints import execution, request_context
from gobby.storage.agents import AgentRun, AgentRunStatus
from gobby.utils.session_context import (
    AGENT_RUN_ID_HEADER,
    SeededContextTokens,
    SessionContext,
    get_current_agent_run_id,
    get_current_session_id,
    set_session_context,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
_TOOL_PATH = "/api/mcp/gobby-tasks/tools/get_task"


@dataclass
class AgentRunClient:
    client: AsyncClient
    run: AgentRun
    manager: MagicMock
    dispatch: AsyncMock
    headers: dict[str, str]


@pytest.fixture
async def agent_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AgentRunClient]:
    now = datetime.now(UTC)
    run = AgentRun(
        id=str(uuid4()),
        parent_session_id=str(uuid4()),
        child_session_id=str(uuid4()),
        machine_id="test-machine",
        provider="codex",
        prompt="Inspect the assigned task.",
        agent_name="researcher",
        status="running",
        created_at=now,
        updated_at=now,
    )
    manager = MagicMock()
    manager.get.return_value = run
    manager.get_by_session.return_value = None
    monkeypatch.setattr(request_context, "LocalAgentRunManager", lambda db: manager)
    project_id = str(uuid4())

    async def seed_contexts(**kwargs: Any) -> SeededContextTokens:
        session_id = kwargs["session_ref"]
        return SeededContextTokens(
            resolved_session_id=session_id,
            resolved_project_id=project_id,
            session_token=set_session_context(
                SessionContext(session_id) if session_id is not None else None
            ),
        )

    monkeypatch.setattr(request_context, "resolve_and_seed_contexts", seed_contexts)
    monkeypatch.setattr(execution, "_http_request_scope", lambda *args: project_id)

    async def dispatch(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "success": True,
            "run_id": get_current_agent_run_id(),
            "session_id": get_current_session_id(),
        }

    server = MagicMock()
    server.auth_service.verified_agent_claims.return_value = None
    server.run_db = AsyncMock(
        side_effect=lambda function, *args, **kwargs: function(*args, **kwargs)
    )
    server.tool_proxy.call_tool = AsyncMock(side_effect=dispatch)
    server.config.mcp_client_proxy.tool_timeout = 10.0
    app = FastAPI()
    app.dependency_overrides[get_server] = lambda: server
    app.add_api_route(
        "/api/mcp/{server_name}/tools/{tool_name}", execution.mcp_proxy, methods=["POST"]
    )
    headers = {
        AGENT_RUN_ID_HEADER: run.id,
        "x-gobby-session-id": str(run.child_session_id),
        "x-gobby-project-id": project_id,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield AgentRunClient(client, run, manager, server.tool_proxy.call_tool, headers)


@pytest.mark.parametrize("status", ["pending", "running"])
async def test_active_runs_retain_ordinary_tool_access(
    agent_client: AgentRunClient, status: AgentRunStatus
) -> None:
    case = agent_client
    case.run.status = status
    response = await case.client.post(_TOOL_PATH, headers=case.headers, json={"task_id": "#42"})
    assert response.status_code == 200
    case.dispatch.assert_awaited_once()
    assert response.json()["run_id"] == case.run.id
    assert response.json()["session_id"] == case.run.child_session_id
    assert get_current_agent_run_id() is None
    assert get_current_session_id() is None


@pytest.mark.parametrize("status", ["queued", "success", "error", "timeout", "cancelled"])
async def test_inactive_runs_cannot_call_tools(
    agent_client: AgentRunClient, status: AgentRunStatus
) -> None:
    case = agent_client
    case.run.status = status
    response = await case.client.post(_TOOL_PATH, headers=case.headers, json={"task_id": "#42"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid agent run identity"
    case.dispatch.assert_not_awaited()
    assert get_current_agent_run_id() is None
    assert get_current_session_id() is None


@pytest.mark.parametrize("identity", ["malformed", "unknown", "wrong-session"])
async def test_unbound_run_identity_is_rejected(
    agent_client: AgentRunClient, identity: str
) -> None:
    case = agent_client
    if identity == "malformed":
        case.headers[AGENT_RUN_ID_HEADER] = "not-a-uuid"
    elif identity == "unknown":
        case.manager.get.return_value = None
    else:
        case.headers["x-gobby-session-id"] = str(uuid4())
    response = await case.client.post(_TOOL_PATH, headers=case.headers, json={"task_id": "#42"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid agent run identity"
    case.dispatch.assert_not_awaited()
    assert get_current_agent_run_id() is None
    assert get_current_session_id() is None
