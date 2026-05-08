"""Schema-guided invalid argument responses for ToolProxyService."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit


@pytest.fixture
def temp_db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_tool_proxy_invalid_arguments_schema.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def proxy_parts(temp_db: LocalDatabase):
    mcp_manager = MagicMock()
    mcp_manager.project_id = "test-project"
    mcp_manager.has_server.return_value = True
    mcp_manager.call_tool = AsyncMock(return_value={"success": True})
    mcp_manager.get_tool_input_schema = AsyncMock()

    internal_manager = MagicMock()
    internal_manager.is_internal.return_value = False

    hook_manager = SimpleNamespace(_database=temp_db, _session_manager=None)
    proxy = ToolProxyService(
        mcp_manager=mcp_manager,
        internal_manager=internal_manager,
        validate_arguments=True,
        hook_manager_resolver=lambda: hook_manager,
    )
    return proxy, mcp_manager, temp_db


def _schema_response(tool_name: str, input_schema: dict):
    return {
        "success": True,
        "tool": {
            "name": tool_name,
            "inputSchema": input_schema,
        },
    }


@pytest.mark.asyncio
async def test_first_invalid_call_includes_schema_and_records_latch(proxy_parts) -> None:
    proxy, mcp_manager, db = proxy_parts
    input_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    mcp_manager.get_tool_input_schema.return_value = _schema_response("test_tool", input_schema)

    result = await proxy.call_tool(
        "test-server",
        "test_tool",
        {"wrong": "value"},
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["error_code"] == "invalid_arguments"
    assert result["schema"] == input_schema
    assert result["validation_errors"] == [
        "Unknown parameter 'wrong'. Valid parameters: ['name']",
        "Missing required parameter 'name'",
    ]
    variables = SessionVariableManager(db).get_variables("session-1")
    assert "test-server:test_tool" in variables["unlocked_tools"]


@pytest.mark.asyncio
async def test_second_invalid_call_omits_schema(proxy_parts) -> None:
    proxy, mcp_manager, _db = proxy_parts
    input_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    mcp_manager.get_tool_input_schema.return_value = _schema_response("test_tool", input_schema)

    await proxy.call_tool(
        "test-server",
        "test_tool",
        {"wrong": "value"},
        session_id="session-2",
    )
    result = await proxy.call_tool(
        "test-server",
        "test_tool",
        {"wrong": "value"},
        session_id="session-2",
    )

    assert result["success"] is False
    assert "schema" not in result
    assert result["schema_already_shown"] is True


@pytest.mark.asyncio
async def test_leaked_routing_fields_are_invalid_target_arguments(proxy_parts) -> None:
    proxy, mcp_manager, _db = proxy_parts
    input_schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": [],
    }
    mcp_manager.get_tool_input_schema.return_value = _schema_response("create_task", input_schema)

    result = await proxy.call_tool(
        "gobby-tasks",
        "create_task",
        {
            "title": "Fix validation",
            "server_name": "gobby-tasks",
            "tool_name": "create_task",
        },
        session_id="session-leaked",
    )

    assert result["success"] is False
    assert "Unknown parameter 'server_name'" in result["error"]
    assert "Unknown parameter 'tool_name'" in result["error"]
    assert result["schema"] == input_schema


@pytest.mark.asyncio
async def test_missing_target_session_id_returns_schema_once(proxy_parts) -> None:
    proxy, mcp_manager, _db = proxy_parts
    input_schema = {
        "type": "object",
        "properties": {"session_id": {"type": "string"}},
        "required": ["session_id"],
    }
    mcp_manager.get_tool_input_schema.return_value = _schema_response("needs_session", input_schema)

    result = await proxy.call_tool(
        "gobby-sessions",
        "needs_session",
        {},
        session_id="session-wrapper",
    )

    assert result["success"] is False
    assert result["error_code"] == "invalid_arguments"
    assert "arguments.session_id" in result["error"]
    assert result["schema"] == input_schema

    second = await proxy.call_tool(
        "gobby-sessions",
        "needs_session",
        {},
        session_id="session-wrapper",
    )
    assert "schema" not in second
    assert second["schema_already_shown"] is True


@pytest.mark.asyncio
async def test_malformed_string_arguments_return_schema_guidance(proxy_parts) -> None:
    proxy, mcp_manager, _db = proxy_parts
    input_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    mcp_manager.get_tool_input_schema.return_value = _schema_response("test_tool", input_schema)

    result = await proxy.call_tool(
        "test-server",
        "test_tool",
        "not valid json {",
        session_id="session-string",
    )

    assert result["success"] is False
    assert result["error_code"] == "invalid_arguments"
    assert "expected dict" in result["error"]
    assert result["validation_errors"] == [result["error"]]
    assert result["schema"] == input_schema
    mcp_manager.call_tool.assert_not_awaited()
