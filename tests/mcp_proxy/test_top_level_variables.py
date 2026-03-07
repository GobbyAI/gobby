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
        session_manager=session_manager,
    )


@pytest.mark.asyncio
async def test_set_variable_delegates_correctly() -> None:
    sm = MagicMock()
    sm.db = MagicMock()
    handler = _make_handler(session_manager=sm)

    with patch(
        "gobby.mcp_proxy.tools.workflows._variables.set_variable",
        return_value={"ok": True, "value": True, "scope": "session"},
    ) as mock_set:
        result = await handler.set_variable(
            name="flag", value=True, session_id="#1"
        )

    mock_set.assert_called_once_with(
        sm, sm.db, "flag", True, "#1", workflow=None
    )
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_get_variable_delegates_correctly() -> None:
    sm = MagicMock()
    sm.db = MagicMock()
    handler = _make_handler(session_manager=sm)

    with patch(
        "gobby.mcp_proxy.tools.workflows._variables.get_variable",
        return_value={"ok": True, "session_id": "uuid", "variable": "flag", "value": True, "exists": True, "scope": "session"},
    ) as mock_get:
        result = await handler.get_variable(name="flag", session_id="#1")

    mock_get.assert_called_once_with(
        sm, sm.db, "flag", "#1", workflow=None
    )
    assert result["ok"] is True
    assert result["value"] is True


@pytest.mark.asyncio
async def test_set_variable_no_session_manager() -> None:
    handler = _make_handler(session_manager=None)
    result = await handler.set_variable(name="x", value=1, session_id="#1")
    assert result["ok"] is False
    assert "Session manager" in result["error"]


@pytest.mark.asyncio
async def test_get_variable_no_session_manager() -> None:
    handler = _make_handler(session_manager=None)
    result = await handler.get_variable(name="x", session_id="#1")
    assert result["ok"] is False
    assert "Session manager" in result["error"]


def test_tools_registered() -> None:
    handler = _make_handler(session_manager=MagicMock())
    mcp = create_mcp_server(handler)
    tool_names = [t.name for t in mcp._tool_manager.list_tools()]
    assert "set_variable" in tool_names
    assert "get_variable" in tool_names
