"""Tests for top-level set_variable / get_variable on GobbyDaemonTools."""

from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.server import GobbyDaemonTools, create_mcp_server

pytestmark = pytest.mark.unit


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

    with patch(
        "gobby.mcp_proxy.tools.workflows._variables.set_variable",
        return_value={"success": True, "value": True, "scope": "session"},
    ) as mock_set:
        result = await handler.set_variable(
            name="flag",
            value=True,
            session_id="#1",
            scope="step",
        )

    mock_set.assert_called_once()
    args, kwargs = mock_set.call_args
    assert args[:5] == (sm, sm.db, "flag", True, "#1")
    assert kwargs["scope"] == "step"
    assert result["success"] is True


@pytest.mark.asyncio
async def test_get_variable_delegates_correctly() -> None:
    sm = MagicMock()
    sm.db = MagicMock()
    handler = _make_handler(session_manager=sm)

    with patch(
        "gobby.mcp_proxy.tools.workflows._variables.get_variable",
        return_value={
            "success": True,
            "session_id": "uuid",
            "variable": "flag",
            "value": True,
            "exists": True,
            "scope": "session",
        },
    ) as mock_get:
        result = await handler.get_variable(
            name="flag",
            session_id="#1",
            scope="step",
        )

    mock_get.assert_called_once()
    args, kwargs = mock_get.call_args
    assert args[:4] == (sm, sm.db, "flag", "#1")
    assert kwargs["scope"] == "step"
    assert result["success"] is True
    assert result["value"] is True


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


def test_tools_registered() -> None:
    handler = _make_handler(session_manager=MagicMock())
    mcp = create_mcp_server(handler)
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

    for tool_name in ("set_variable", "get_variable"):
        assert tool_name in tools
        scope_schema = tools[tool_name].parameters["properties"]["scope"]
        assert scope_schema["default"] == "session"
