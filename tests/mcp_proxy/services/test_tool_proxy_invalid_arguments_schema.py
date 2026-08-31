"""Schema-guided invalid argument responses for ToolProxyService."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000016"

ProxyParts = tuple[ToolProxyService, MagicMock, HubDatabase, str]


def _attach_named_servers(manager: MagicMock, *names: str) -> None:
    from gobby.mcp_proxy.models import MCPServerConfig
    from gobby.storage.projects import GLOBAL_PROJECT_ID

    configs = [
        MCPServerConfig(
            name=name,
            project_id=GLOBAL_PROJECT_ID,
            url="https://example.test",
            id=name,
        )
        for name in names
    ]
    manager.server_configs = configs
    manager._configs = {config.id: config for config in configs}
    manager.get_server_config.side_effect = lambda sid: manager._configs.get(sid)
    manager.has_server.side_effect = lambda sid: sid in manager._configs


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def temp_db(postgres_db: HubDatabase) -> HubDatabase:
    return postgres_db


@pytest.fixture
def proxy_parts(temp_db: HubDatabase) -> ProxyParts:
    session = SessionManager(temp_db).register(
        external_id="schema-guidance-test-session",
        machine_id="21000000-0000-4000-8000-000000000016",
        source="codex",
        project_id=None,
        title="Schema guidance test session",
    )
    mcp_manager = MagicMock()
    mcp_manager.project_id = "test-project"
    mcp_manager.call_tool = AsyncMock(return_value={"success": True})
    mcp_manager.get_tool_input_schema = AsyncMock()
    _attach_named_servers(mcp_manager, "test-server", "gobby-tasks", "gobby-sessions")

    internal_manager = MagicMock()
    internal_manager.is_internal.return_value = False

    hook_manager = cast(
        "HookManager",
        SimpleNamespace(_database=temp_db, _session_manager=None),
    )
    proxy = ToolProxyService(
        mcp_manager=mcp_manager,
        internal_manager=internal_manager,
        validate_arguments=True,
        hook_manager_resolver=lambda: hook_manager,
    )
    return proxy, mcp_manager, temp_db, session.id


def _manager_schema(input_schema: dict[str, Any]) -> dict[str, Any]:
    """Match MCPClientManager.get_tool_input_schema's bare-schema contract."""
    return input_schema


@pytest.mark.asyncio
async def test_first_invalid_call_includes_schema_and_records_latch(
    proxy_parts: ProxyParts,
) -> None:
    proxy, mcp_manager, db, session_id = proxy_parts
    input_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    mcp_manager.get_tool_input_schema.return_value = _manager_schema(input_schema)

    result = await proxy.call_tool(
        "test-server",
        "test_tool",
        {"wrong": "value"},
        session_id=session_id,
    )

    assert result["success"] is False
    assert result["error_code"] == "INVALID_ARGUMENTS"
    assert result["schema"] == input_schema
    assert result["validation_errors"] == [
        "Unknown parameter 'wrong'. Valid parameters: ['name']",
        "Missing required parameter 'name'",
    ]
    variables = SessionVariableManager(db).get_variables(session_id)
    assert "test-server:test_tool" in variables["unlocked_tools"]


@pytest.mark.asyncio
async def test_repeated_invalid_call_includes_schema_and_retains_lease(
    proxy_parts: ProxyParts,
) -> None:
    proxy, mcp_manager, db, session_id = proxy_parts
    input_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    mcp_manager.get_tool_input_schema.return_value = _manager_schema(input_schema)

    await proxy.call_tool(
        "test-server",
        "test_tool",
        {"wrong": "value"},
        session_id=session_id,
    )
    result = await proxy.call_tool(
        "test-server",
        "test_tool",
        {"wrong": "value"},
        session_id=session_id,
    )

    assert result["success"] is False
    assert result["schema"] == input_schema
    variables = SessionVariableManager(db).get_variables(session_id)
    assert variables["unlocked_tools"].count("test-server:test_tool") == 1


@pytest.mark.asyncio
async def test_leaked_routing_fields_are_invalid_target_arguments(
    proxy_parts: ProxyParts,
) -> None:
    proxy, mcp_manager, _db, session_id = proxy_parts
    input_schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": [],
    }
    mcp_manager.get_tool_input_schema.return_value = _manager_schema(input_schema)

    result = await proxy.call_tool(
        "gobby-tasks",
        "create_task",
        {
            "title": "Fix validation",
            "server_name": "gobby-tasks",
            "tool_name": "create_task",
        },
        session_id=session_id,
    )

    assert result["success"] is False
    assert "Unknown parameter 'server_name'" in result["error"]
    assert "Unknown parameter 'tool_name'" in result["error"]
    assert result["schema"] == input_schema


@pytest.mark.asyncio
async def test_required_session_id_injected_from_wrapper_context(
    proxy_parts: ProxyParts,
) -> None:
    proxy, mcp_manager, _db, session_id = proxy_parts
    input_schema = {
        "type": "object",
        "properties": {"session_id": {"type": "string"}},
        "required": ["session_id"],
    }
    mcp_manager.get_tool_input_schema.return_value = _manager_schema(input_schema)

    result = await proxy.call_tool(
        "gobby-sessions",
        "needs_session",
        {},
        session_id=session_id,
    )

    assert result["success"] is True
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-sessions",
        tool_name="needs_session",
        arguments={"session_id": session_id},
        session_id=session_id,
    )


@pytest.mark.asyncio
async def test_malformed_string_arguments_return_schema_guidance(
    proxy_parts: ProxyParts,
) -> None:
    proxy, mcp_manager, _db, session_id = proxy_parts
    input_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    mcp_manager.get_tool_input_schema.return_value = _manager_schema(input_schema)

    result = await proxy.call_tool(
        "test-server",
        "test_tool",
        "not valid json {",
        session_id=session_id,
    )

    assert result["success"] is False
    assert result["error_code"] == "INVALID_ARGUMENTS"
    assert "expected dict" in result["error"]
    assert result["validation_errors"] == [result["error"]]
    assert result["schema"] == input_schema
    mcp_manager.call_tool.assert_not_awaited()
