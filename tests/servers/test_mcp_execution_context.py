"""Trusted agent-run identity across stdio and HTTP MCP execution."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from gobby.mcp_proxy.stdio_proxy import DaemonProxy, DaemonProxyDependencies
from gobby.servers.routes.mcp.endpoints.execution import (
    _reset_context,
    _set_context_for_request,
)
from gobby.utils.session_context import SeededContextTokens


def _request(**headers: str) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
        }
    )


@pytest.mark.asyncio
async def test_run_identity_transport_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.utils.session_context import (
        AGENT_RUN_ID_HEADER,
        get_current_agent_run_id,
    )

    monkeypatch.setenv("GOBBY_SESSION_ID", "session-1")
    monkeypatch.setenv("GOBBY_AGENT_RUN_ID", "run-1")
    response = MagicMock(status_code=200)
    response.json.return_value = {"success": True}
    client = MagicMock()
    client.request = AsyncMock(return_value=response)
    client.aclose = AsyncMock()
    deps = DaemonProxyDependencies(
        load_config=MagicMock(),
        check_daemon_http_health=AsyncMock(return_value=True),
        read_project_id=lambda: "project-1",
        resolve_session_id_from_terminal_context=AsyncMock(return_value=None),
        http_client_factory=lambda: client,
        logger=logging.getLogger("test"),
    )
    proxy = DaemonProxy(60887, deps_factory=lambda: deps)

    assert await proxy._request("POST", "/api/mcp/gobby-agents/end_agent_run") == {"success": True}
    request_headers = client.request.await_args.kwargs["headers"]
    assert request_headers[AGENT_RUN_ID_HEADER] == "run-1"

    server = MagicMock()
    server.session_manager.db = MagicMock()
    server.run_db = AsyncMock(side_effect=lambda operation, *args: operation(*args))
    run = SimpleNamespace(id="run-1", child_session_id="session-1", status="running")
    run_manager = MagicMock()
    run_manager.get.return_value = run
    run_manager.get_by_session.return_value = run
    seeded = SeededContextTokens(resolved_session_id="session-1")
    valid_request = _request(
        **{
            "X-Gobby-Session-Id": "session-1",
            AGENT_RUN_ID_HEADER: "run-1",
            "X-Gobby-Project-Id": "project-1",
        }
    )

    with (
        patch(
            "gobby.servers.routes.mcp.endpoints.execution.resolve_and_seed_contexts",
            new_callable=AsyncMock,
            return_value=seeded,
        ),
        patch(
            "gobby.servers.routes.mcp.endpoints.execution.LocalAgentRunManager",
            return_value=run_manager,
        ),
    ):
        tokens = await _set_context_for_request(server, {}, valid_request)
        assert get_current_agent_run_id() == "run-1"
        _reset_context(tokens)
        assert get_current_agent_run_id() is None

        absent = _request(
            **{
                "X-Gobby-Session-Id": "session-1",
                "X-Gobby-Project-Id": "project-1",
            }
        )
        with pytest.raises(HTTPException, match="agent run identity"):
            await _set_context_for_request(server, {}, absent)

        run_manager.get.return_value = None
        forged = _request(
            **{
                "X-Gobby-Session-Id": "session-1",
                AGENT_RUN_ID_HEADER: "run-forged",
                "X-Gobby-Project-Id": "project-1",
            }
        )
        with pytest.raises(HTTPException, match="agent run identity"):
            await _set_context_for_request(server, {}, forged)

        run_manager.get.return_value = SimpleNamespace(
            id="run-other",
            child_session_id="session-other",
            status="running",
        )
        mismatched = _request(
            **{
                "X-Gobby-Session-Id": "session-1",
                AGENT_RUN_ID_HEADER: "run-other",
                "X-Gobby-Project-Id": "project-1",
            }
        )
        with pytest.raises(HTTPException, match="agent run identity"):
            await _set_context_for_request(server, {}, mismatched)
