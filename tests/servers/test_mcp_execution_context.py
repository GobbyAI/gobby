"""Trusted agent-run identity across stdio and HTTP MCP execution."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from gobby.mcp_proxy.wait_tools import (
    MCP_WRAPPER_PROTOCOL_VERSION,
    MCP_WRAPPER_PROTOCOL_VERSION_HEADER,
)
from gobby.servers.routes.mcp.endpoints.request_context import (
    _reset_context,
    _set_context_for_request,
)
from gobby.utils.session_context import SeededContextTokens

WRAPPER_CONTRACT = Path(__file__).resolve().parents[2] / "crates/gmcp/contracts/mcp_wrapper.v1.json"


def _request(**headers: str) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
        }
    )


def _wrapper_identity_headers(env: dict[str, str]) -> dict[str, str]:
    """Headers the gmcp wrapper sends for identity env, per its checked-in contract."""
    contract = json.loads(WRAPPER_CONTRACT.read_text())
    return {
        header: env[name] for name, header in contract["agent_identity_env_headers"] if name in env
    }


@pytest.mark.asyncio
async def test_run_identity_transport_chain() -> None:
    from gobby.utils.session_context import (
        AGENT_RUN_ID_HEADER,
        get_current_agent_run_id,
    )

    wrapper_headers = _wrapper_identity_headers(
        {
            "GOBBY_SESSION_ID": "session-1",
            "GOBBY_AGENT_RUN_ID": "21000000-0000-4000-8000-000000000071",
        }
    )
    assert wrapper_headers[AGENT_RUN_ID_HEADER] == "21000000-0000-4000-8000-000000000071"

    server = MagicMock()
    server.session_manager.db = MagicMock()
    server.auth_service.verified_agent_claims.return_value = None
    server.run_db = AsyncMock(side_effect=lambda operation, *args: operation(*args))
    run = SimpleNamespace(
        id="21000000-0000-4000-8000-000000000071", child_session_id="session-1", status="running"
    )
    run_manager = MagicMock()
    run_manager.get.return_value = run
    run_manager.get_by_session.return_value = run
    seeded = SeededContextTokens(resolved_session_id="session-1")
    valid_request = _request(
        **{
            **wrapper_headers,
            "X-Gobby-Project-Id": "project-1",
            MCP_WRAPPER_PROTOCOL_VERSION_HEADER: MCP_WRAPPER_PROTOCOL_VERSION,
        }
    )

    with (
        patch(
            "gobby.servers.routes.mcp.endpoints.request_context.resolve_and_seed_contexts",
            new_callable=AsyncMock,
            return_value=seeded,
        ),
        patch(
            "gobby.servers.routes.mcp.endpoints.request_context.LocalAgentRunManager",
            return_value=run_manager,
        ),
    ):
        tokens = await _set_context_for_request(server, {}, valid_request)
        assert get_current_agent_run_id() == "21000000-0000-4000-8000-000000000071"
        _reset_context(tokens)
        assert get_current_agent_run_id() is None

        absent = _request(
            **{
                "X-Gobby-Session-Id": "session-1",
                "X-Gobby-Project-Id": "project-1",
                MCP_WRAPPER_PROTOCOL_VERSION_HEADER: MCP_WRAPPER_PROTOCOL_VERSION,
            }
        )
        with pytest.raises(HTTPException, match="agent run identity"):
            await _set_context_for_request(server, {}, absent)

        run_manager.get.return_value = None
        forged = _request(
            **{
                "X-Gobby-Session-Id": "session-1",
                AGENT_RUN_ID_HEADER: "21000000-0000-4000-8000-000000000072",
                "X-Gobby-Project-Id": "project-1",
                MCP_WRAPPER_PROTOCOL_VERSION_HEADER: MCP_WRAPPER_PROTOCOL_VERSION,
            }
        )
        with pytest.raises(HTTPException, match="agent run identity"):
            await _set_context_for_request(server, {}, forged)

        run_manager.get.return_value = SimpleNamespace(
            id="21000000-0000-4000-8000-000000000073",
            child_session_id="session-other",
            status="running",
        )
        mismatched = _request(
            **{
                "X-Gobby-Session-Id": "session-1",
                AGENT_RUN_ID_HEADER: "21000000-0000-4000-8000-000000000073",
                "X-Gobby-Project-Id": "project-1",
                MCP_WRAPPER_PROTOCOL_VERSION_HEADER: MCP_WRAPPER_PROTOCOL_VERSION,
            }
        )
        with pytest.raises(HTTPException, match="agent run identity"):
            await _set_context_for_request(server, {}, mismatched)
