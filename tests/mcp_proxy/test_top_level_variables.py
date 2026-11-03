"""Tests for top-level set_variable / get_variable on GobbyDaemonTools."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.server import GobbyDaemonTools, create_mcp_server
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import SessionContext
from gobby.workflows.state_manager import SessionVariableManager
from tests.mcp_proxy.caller_principal_support import (
    bound_principal,
    operator_caller,
    session_claims,
)

pytestmark = pytest.mark.unit

RESOLVED = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@contextmanager
def _operator_naming_resolved_session() -> Iterator[None]:
    """The operator names "#1", which resolves to RESOLVED."""
    project = "11111111-1111-1111-1111-111111111111"
    resolved = (RESOLVED, project, SessionContext(session_id=RESOLVED), {"id": project})
    with (
        operator_caller(),
        patch("gobby.utils.session_context._resolve_context_values", return_value=resolved),
    ):
        yield


def _make_handler(session_manager: MagicMock | None = None) -> GobbyDaemonTools:
    """Build a GobbyDaemonTools with minimal mocks."""
    mcp_manager = MagicMock()
    mcp_manager.server_configs = []
    mcp_manager.connections = {}
    mcp_manager.health = {}
    mcp_manager.project_id = None

    return GobbyDaemonTools(
        mcp_manager=mcp_manager,
        daemon_port=60887,
        websocket_port=60888,
        start_time=0.0,
        internal_manager=None,
        db=MagicMock(),
        session_manager=session_manager,
    )


@pytest.mark.asyncio
async def test_set_variable_delegates_correctly() -> None:
    sm = MagicMock()
    sm.db = MagicMock()
    handler = _make_handler(session_manager=sm)

    with (
        _operator_naming_resolved_session(),
        patch(
            "gobby.mcp_proxy.tools.workflows._variables.set_variable",
            return_value={"success": True, "value": True, "scope": "session"},
        ) as mock_set,
    ):
        result = await handler.set_variable(
            name="flag",
            value=True,
            session_id="#1",
            scope="step",
        )

    mock_set.assert_called_once()
    args, kwargs = mock_set.call_args
    assert args[:5] == (sm, sm.db, "flag", True, RESOLVED)
    assert kwargs["scope"] == "step"
    assert result["success"] is True


@pytest.mark.asyncio
async def test_get_variable_delegates_correctly() -> None:
    sm = MagicMock()
    sm.db = MagicMock()
    handler = _make_handler(session_manager=sm)

    with (
        _operator_naming_resolved_session(),
        patch(
            "gobby.mcp_proxy.tools.workflows._variables.get_variable",
            return_value={
                "success": True,
                "session_id": "uuid",
                "variable": "flag",
                "value": True,
                "exists": True,
                "scope": "session",
            },
        ) as mock_get,
    ):
        result = await handler.get_variable(
            name="flag",
            session_id="#1",
            scope="step",
        )

    mock_get.assert_called_once()
    args, kwargs = mock_get.call_args
    assert args[:4] == (sm, sm.db, "flag", RESOLVED)
    assert kwargs["scope"] == "step"
    assert result["success"] is True
    assert result["value"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("kwargs", "brief"), [({}, True), ({"brief": False}, False)])
async def test_get_variable_forwards_brief_to_the_workflow_tool(
    kwargs: dict[str, Any], brief: bool
) -> None:
    sm = MagicMock()
    sm.db = MagicMock()
    handler = _make_handler(session_manager=sm)

    with (
        _operator_naming_resolved_session(),
        patch(
            "gobby.mcp_proxy.tools.workflows._variables.get_variable",
            return_value={"success": True, "names": []},
        ) as mock_get,
    ):
        await handler.get_variable(session_id="#1", **kwargs)

    assert mock_get.call_args.kwargs["brief"] is brief


@pytest.mark.asyncio
async def test_set_variable_no_session_manager() -> None:
    handler = _make_handler(session_manager=None)
    result = await handler.set_variable(name="x", value=1, session_id="#1")
    assert result["success"] is False
    assert "Session manager" in result["error"]


@pytest.mark.asyncio
async def test_get_variable_no_session_manager() -> None:
    handler = _make_handler(session_manager=None)
    result = await handler.get_variable(name="x", session_id="#1")
    assert result["success"] is False
    assert "Session manager" in result["error"]


@pytest.mark.asyncio
async def test_managed_caller_sets_its_own_session_variable_end_to_end(
    temp_db: HubDatabase,
) -> None:
    """/mcp seeds no ambient session; the gated caller must reach the write authority."""
    project = "a3333333-3333-4333-8333-333333333333"
    caller = "a1111111-1111-4111-8111-111111111111"
    temp_db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, CURRENT_TIMESTAMP)",
        (project, "top-level-variables"),
    )
    temp_db.execute(
        "INSERT INTO sessions "
        "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (caller, caller, "21000000-0000-4000-8000-000000000001", "codex", project),
    )
    handler = GobbyDaemonTools(
        mcp_manager=MagicMock(server_configs=[], connections={}, health={}, project_id=None),
        daemon_port=60887,
        websocket_port=60888,
        start_time=0.0,
        internal_manager=None,
        db=temp_db,
        session_manager=SessionManager(temp_db),
    )

    with bound_principal(session_claims(caller, project_id=project)):
        result = await handler.set_variable(name="sample", value="value", session_id=caller)

    assert result == {"success": True, "value": "value", "scope": "session"}
    assert SessionVariableManager(temp_db).get_variables(caller)["sample"] == "value"


def test_tools_registered() -> None:
    handler = _make_handler(session_manager=MagicMock())
    mcp = create_mcp_server(handler)
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

    for tool_name in ("set_variable", "get_variable"):
        assert tool_name in tools
        scope_schema = tools[tool_name].parameters["properties"]["scope"]
        assert scope_schema["default"] == "session"
