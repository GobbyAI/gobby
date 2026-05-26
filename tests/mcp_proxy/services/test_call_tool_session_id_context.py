"""Regression tests for call_tool wrapper versus target session_id handling."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


SESSION_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string"},
        "limit": {"type": "integer"},
        "lines": {"type": "integer"},
    },
    "required": ["session_id"],
}

OPTIONAL_SESSION_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"session_id": {"type": "string"}, "name": {"type": "string"}},
    "required": [],
}

SESSION_ID_AND_NAME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"session_id": {"type": "string"}, "name": {"type": "string"}},
    "required": ["session_id", "name"],
}

COMPACT_SELF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"rule_name": {"type": "string"}},
    "required": [],
}

SESSION_UUID_3 = "33333333-3333-4333-8333-333333333333"
SESSION_UUID_7 = "77777777-7777-4777-8777-777777777777"


@pytest.fixture
def tool_proxy() -> tuple[ToolProxyService, MagicMock]:
    mcp_manager = MagicMock()
    mcp_manager.project_id = "proj-1"
    mcp_manager.session_manager = None
    mcp_manager.call_tool = AsyncMock(return_value={"success": True, "ok": True})

    internal_manager = MagicMock()
    internal_manager.is_internal.return_value = False

    proxy = ToolProxyService(
        mcp_manager=mcp_manager,
        internal_manager=internal_manager,
        validate_arguments=True,
    )
    return proxy, mcp_manager


@pytest.fixture
def resolving_tool_proxy() -> tuple[ToolProxyService, MagicMock, MagicMock]:
    mcp_manager = MagicMock()
    mcp_manager.project_id = "proj-1"
    mcp_manager.call_tool = AsyncMock(return_value={"success": True, "ok": True})

    session_manager = MagicMock()

    def resolve_session_reference(ref: str, project_id: str | None = None) -> str:
        return {
            "#3": SESSION_UUID_3,
            "#7": SESSION_UUID_7,
        }[ref]

    session_manager.resolve_session_reference.side_effect = resolve_session_reference

    hook_manager = MagicMock()
    hook_manager._session_manager = session_manager

    internal_manager = MagicMock()
    internal_manager.is_internal.return_value = False

    proxy = ToolProxyService(
        mcp_manager=mcp_manager,
        internal_manager=internal_manager,
        validate_arguments=True,
        hook_manager_resolver=lambda: hook_manager,
    )
    return proxy, mcp_manager, session_manager


def _use_schema(proxy: ToolProxyService, schema: dict[str, Any]) -> None:
    async def get_tool_schema(server_name: str, tool_name: str) -> dict[str, Any]:
        return {"success": True, "tool": {"inputSchema": schema}}

    proxy.get_tool_schema = get_tool_schema


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("get_session", {}),
        ("capture_output", {"lines": 10}),
        ("get_session_messages", {"limit": 25}),
    ],
)
async def test_required_session_id_uses_top_level_wrapper_session(
    tool_proxy: tuple[ToolProxyService, MagicMock],
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    proxy, mcp_manager = tool_proxy

    async def get_tool_schema(server_name: str, requested_tool: str) -> dict[str, Any]:
        assert server_name == "gobby-sessions"
        assert requested_tool == tool_name
        return {"success": True, "tool": {"inputSchema": SESSION_ID_SCHEMA}}

    proxy.get_tool_schema = get_tool_schema

    result = await proxy.call_tool(
        "gobby-sessions",
        tool_name,
        arguments=arguments,
        session_id="wrapper-session",
        enforce_workflow=False,
    )

    expected_arguments = {**arguments, "session_id": "wrapper-session"}
    assert result["success"] is True
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-sessions",
        tool_name,
        expected_arguments,
        session_id="wrapper-session",
    )


@pytest.mark.asyncio
async def test_required_session_id_injection_resolves_wrapper_session_ref(
    resolving_tool_proxy: tuple[ToolProxyService, MagicMock, MagicMock],
) -> None:
    proxy, mcp_manager, _ = resolving_tool_proxy
    _use_schema(proxy, SESSION_ID_SCHEMA)

    result = await proxy.call_tool(
        "gobby-sessions",
        "get_session_messages",
        arguments={"limit": 25},
        session_id="#7",
        enforce_workflow=False,
    )

    assert result["success"] is True
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-sessions",
        "get_session_messages",
        {"limit": 25, "session_id": SESSION_UUID_7},
        session_id=SESSION_UUID_7,
    )


@pytest.mark.asyncio
async def test_required_session_id_uses_ambient_session_context(
    tool_proxy: tuple[ToolProxyService, MagicMock],
) -> None:
    proxy, mcp_manager = tool_proxy
    _use_schema(proxy, SESSION_ID_SCHEMA)

    with session_context_for_test("ambient-session-uuid"):
        result = await proxy.call_tool(
            "gobby-sessions",
            "get_session",
            arguments={},
            enforce_workflow=False,
        )

    assert result["success"] is True
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-sessions",
        "get_session",
        {"session_id": "ambient-session-uuid"},
        session_id="ambient-session-uuid",
    )


@pytest.mark.asyncio
async def test_injected_session_id_does_not_mask_missing_required_parameters(
    tool_proxy: tuple[ToolProxyService, MagicMock],
) -> None:
    proxy, mcp_manager = tool_proxy
    _use_schema(proxy, SESSION_ID_AND_NAME_SCHEMA)

    result = await proxy.call_tool(
        "gobby-sessions",
        "rename_session",
        arguments={},
        session_id="wrapper-session",
        enforce_workflow=False,
    )

    assert result["success"] is False
    assert result["error_code"] == "invalid_arguments"
    assert "Missing required parameter 'name'" in result["error"]
    mcp_manager.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_arguments_session_id_stays_in_target_arguments(
    tool_proxy: tuple[ToolProxyService, MagicMock],
) -> None:
    proxy, mcp_manager = tool_proxy

    async def get_tool_schema(server_name: str, tool_name: str) -> dict[str, Any]:
        return {"success": True, "tool": {"inputSchema": SESSION_ID_SCHEMA}}

    proxy.get_tool_schema = get_tool_schema

    result = await proxy.call_tool(
        "gobby-sessions",
        "get_session",
        arguments={"session_id": "target-session"},
        session_id="wrapper-session",
        enforce_workflow=False,
    )

    assert result["success"] is True
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-sessions",
        "get_session",
        {"session_id": "target-session"},
        session_id="wrapper-session",
    )


@pytest.mark.asyncio
async def test_compact_self_uses_wrapper_session_without_nested_session_id(
    resolving_tool_proxy: tuple[ToolProxyService, MagicMock, MagicMock],
) -> None:
    proxy, mcp_manager, _ = resolving_tool_proxy
    _use_schema(proxy, COMPACT_SELF_SCHEMA)

    result = await proxy.call_tool(
        "gobby-sessions",
        "compact_self",
        arguments={"rule_name": "build-coordinator-handoff"},
        session_id="#7",
        enforce_workflow=False,
    )

    assert result["success"] is True
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-sessions",
        "compact_self",
        {"rule_name": "build-coordinator-handoff"},
        session_id=SESSION_UUID_7,
    )


@pytest.mark.asyncio
async def test_arguments_session_ref_resolves_before_dispatch(
    resolving_tool_proxy: tuple[ToolProxyService, MagicMock, MagicMock],
) -> None:
    proxy, mcp_manager, _ = resolving_tool_proxy
    _use_schema(proxy, OPTIONAL_SESSION_ID_SCHEMA)

    result = await proxy.call_tool(
        "gobby-skills",
        "get_skill",
        arguments={"name": "brevity", "session_id": "#3"},
        enforce_workflow=False,
    )

    assert result["success"] is True
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-skills",
        "get_skill",
        {"name": "brevity", "session_id": SESSION_UUID_3},
        session_id=None,
    )


@pytest.mark.asyncio
async def test_arguments_existing_uuid_passes_through_without_mutation(
    resolving_tool_proxy: tuple[ToolProxyService, MagicMock, MagicMock],
) -> None:
    proxy, mcp_manager, session_manager = resolving_tool_proxy
    _use_schema(proxy, OPTIONAL_SESSION_ID_SCHEMA)

    result = await proxy.call_tool(
        "gobby-skills",
        "get_skill",
        arguments={"name": "brevity", "session_id": SESSION_UUID_3},
        enforce_workflow=False,
    )

    assert result["success"] is True
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-skills",
        "get_skill",
        {"name": "brevity", "session_id": SESSION_UUID_3},
        session_id=None,
    )
    session_manager.resolve_session_reference.assert_not_called()


@pytest.mark.asyncio
async def test_empty_arguments_dispatches_unchanged(
    resolving_tool_proxy: tuple[ToolProxyService, MagicMock, MagicMock],
) -> None:
    proxy, mcp_manager, _ = resolving_tool_proxy

    result = await proxy.call_tool(
        "gobby-skills",
        "list_skills",
        arguments={},
        enforce_workflow=False,
    )

    assert result["success"] is True
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-skills",
        "list_skills",
        {},
        session_id=None,
    )


@pytest.mark.asyncio
async def test_none_arguments_dispatches_empty_dict(
    resolving_tool_proxy: tuple[ToolProxyService, MagicMock, MagicMock],
) -> None:
    proxy, mcp_manager, _ = resolving_tool_proxy

    result = await proxy.call_tool(
        "gobby-skills",
        "list_skills",
        arguments=None,
        enforce_workflow=False,
    )

    assert result["success"] is True
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-skills",
        "list_skills",
        {},
        session_id=None,
    )


@pytest.mark.asyncio
async def test_wrapper_and_nested_session_refs_resolve_independently(
    resolving_tool_proxy: tuple[ToolProxyService, MagicMock, MagicMock],
) -> None:
    proxy, mcp_manager, _ = resolving_tool_proxy
    _use_schema(proxy, SESSION_ID_SCHEMA)

    result = await proxy.call_tool(
        "gobby-sessions",
        "get_session",
        arguments={"session_id": "#7"},
        session_id="#3",
        enforce_workflow=False,
    )

    assert result["success"] is True
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-sessions",
        "get_session",
        {"session_id": SESSION_UUID_7},
        session_id=SESSION_UUID_3,
    )
