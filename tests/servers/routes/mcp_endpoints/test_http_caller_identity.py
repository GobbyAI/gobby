"""Authenticated HTTP caller identity stays separate from tool targets."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

from gobby.mcp_proxy.wait_tools import (
    MCP_WRAPPER_PROTOCOL_VERSION,
    MCP_WRAPPER_PROTOCOL_VERSION_HEADER,
)
from gobby.servers.auth_service import AuthService
from gobby.servers.routes.mcp.endpoints.request_context import (
    _reset_context,
    _set_context_for_request,
)
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.local_token import derive_managed_signing_key, issue_agent_api_token
from gobby.utils.session_context import get_session_context

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MACHINE_ID = "21000000-0000-4000-8000-000000000001"
FRONT_DOOR_SECRET = "test-caller-identity-front-door"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID):
        yield


def _request(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/mcp/tools/call",
            "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        }
    )


@pytest.mark.parametrize("wrapper,explicit_caller", [(False, False), (False, True), (True, True)])
async def test_operator_body_target_never_becomes_caller(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
    wrapper: bool,
    explicit_caller: bool,
) -> None:
    caller = session_manager.register(
        external_id="http-caller",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
    )
    target = session_manager.register(
        external_id="http-target",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
    )
    service = AuthService(lambda: temp_db, break_glass_file=tmp_path / "absent-break-glass")
    service.bind_runtime(
        grant_service=None,
        local_machine_id=MACHINE_ID,
        clock=None,
        front_door_secret=FRONT_DOOR_SECRET,
    )
    # The Rust front door verifies the operator API key and forwards this private identity.
    identity = {
        "X-Gobby-User-Id": "operator-user",
        "X-Gobby-Machine-Id": MACHINE_ID,
        "X-Gobby-Key-Id": "operator-key",
        "X-Gobby-Front-Door": FRONT_DOOR_SECRET,
    }
    headers = {"X-Gobby-Project-Id": sample_project["id"]}
    if explicit_caller:
        headers["X-Gobby-Session-Id"] = caller.id
    if wrapper:
        headers[MCP_WRAPPER_PROTOCOL_VERSION_HEADER] = MCP_WRAPPER_PROTOCOL_VERSION
    assert not service.authenticate(_request(headers)).allowed
    request = _request(headers | identity)
    assert service.authenticate(request).allowed
    server = MagicMock()
    server.session_manager = session_manager
    server.auth_service = service
    server.run_db = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))
    assert get_session_context() is None
    arguments = {"session_id": target.id}
    tokens = await _set_context_for_request(server, arguments, request)
    try:
        context = get_session_context()
        if explicit_caller:
            assert context is not None
            assert context.session_id == caller.id
            assert tokens.resolved_session_id == caller.id
        else:
            assert context is None
            assert tokens.resolved_session_id is None
        assert arguments == {"session_id": target.id}
    finally:
        _reset_context(tokens)
    assert get_session_context() is None


async def test_managed_wrapper_header_is_bound_to_signed_caller(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    caller = session_manager.register(
        external_id="managed-http-caller",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
    )
    target = session_manager.register(
        external_id="managed-http-target",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
    )
    with patch("gobby.storage.agents._lifecycle.get_machine_id", return_value=MACHINE_ID):
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=target.id,
            child_session_id=caller.id,
            provider="claude",
            prompt="HTTP caller identity regression",
        )
    bootstrap = tmp_path / "bootstrap.yaml"
    bootstrap.write_text("api_key: operator-test-key\n")
    service = AuthService(
        lambda: temp_db,
        bootstrap_file=bootstrap,
        break_glass_file=tmp_path / "absent-break-glass",
    )
    token = issue_agent_api_token(
        derive_managed_signing_key("operator-test-key"),
        agent_run_id=run.id,
        session_id=caller.id,
        project_id=sample_project["id"],
    )
    headers = {
        "X-Gobby-Agent-Run-Id": run.id,
        "X-Gobby-Session-Id": caller.id,
        "X-Gobby-Project-Id": sample_project["id"],
        MCP_WRAPPER_PROTOCOL_VERSION_HEADER: MCP_WRAPPER_PROTOCOL_VERSION,
    }
    assert not service.authenticate(_request(headers)).allowed
    authenticated = headers | {"Authorization": f"Bearer {token}"}
    valid = _request(authenticated)
    assert service.authenticate(valid).allowed
    claims = service.verified_agent_claims(valid)
    assert claims is not None
    assert (claims.session_id, claims.agent_run_id) == (caller.id, run.id)
    forged = _request(authenticated | {"X-Gobby-Session-Id": target.id})
    assert not service.authenticate(forged).allowed
    assert service.verified_agent_claims(forged) is None
    server = MagicMock()
    server.session_manager = session_manager
    server.auth_service = service
    server.run_db = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))
    tokens = await _set_context_for_request(server, {"session_id": target.id}, valid)
    try:
        context = get_session_context()
        assert context is not None
        assert context.session_id == caller.id
    finally:
        _reset_context(tokens)
