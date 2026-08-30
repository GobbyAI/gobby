"""Regression tests for call_tool wrapper versus target session_id handling."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.utils.project_context import reset_project_context, set_project_context
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


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


PROJECT_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project_id": {"type": "string"},
        "run_id": {"type": "string"},
    },
    "required": ["project_id"],
}

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
    _attach_named_servers(mcp_manager, "gobby-sessions", "gobby-skills", "gobby-tasks-ops")

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
    _attach_named_servers(mcp_manager, "gobby-sessions", "gobby-skills", "gobby-tasks-ops")

    session_manager = MagicMock()
    # ToolProxyService.session_manager prefers the manager's session_manager;
    # leaving it as an auto-MagicMock would shadow this stub.
    mcp_manager.session_manager = session_manager

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

    object.__setattr__(proxy, "get_tool_schema", get_tool_schema)


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

    object.__setattr__(proxy, "get_tool_schema", get_tool_schema)

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
        tool_name=tool_name,
        arguments=expected_arguments,
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
        tool_name="get_session_messages",
        arguments={"limit": 25, "session_id": SESSION_UUID_7},
        session_id=SESSION_UUID_7,
    )


@pytest.mark.asyncio
async def test_unresolvable_explicit_wrapper_session_is_rejected(
    resolving_tool_proxy: tuple[ToolProxyService, MagicMock, MagicMock],
) -> None:
    proxy, mcp_manager, session_manager = resolving_tool_proxy
    session_manager.resolve_session_reference.side_effect = ValueError("Session not found")
    proxy._tool_filter = MagicMock()
    _use_schema(proxy, {"type": "object", "properties": {}})

    result = await proxy.call_tool(
        "gobby-sessions",
        "get_session_messages",
        arguments={},
        session_id="#99999",
    )

    assert result["success"] is False
    assert result["error_code"] == "INVALID_ARGUMENTS"
    assert "Invalid session reference '#99999'" in result["error"]
    mcp_manager.call_tool.assert_not_awaited()
    proxy._tool_filter.is_tool_allowed.assert_not_called()


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
        tool_name="get_session",
        arguments={"session_id": "ambient-session-uuid"},
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
    assert result["error_code"] == "INVALID_ARGUMENTS"
    assert "Missing required parameter 'name'" in result["error"]
    mcp_manager.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_arguments_session_id_stays_in_target_arguments(
    tool_proxy: tuple[ToolProxyService, MagicMock],
) -> None:
    proxy, mcp_manager = tool_proxy

    async def get_tool_schema(server_name: str, tool_name: str) -> dict[str, Any]:
        return {"success": True, "tool": {"inputSchema": SESSION_ID_SCHEMA}}

    object.__setattr__(proxy, "get_tool_schema", get_tool_schema)

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
        tool_name="get_session",
        arguments={"session_id": "target-session"},
        session_id="wrapper-session",
    )


@pytest.mark.asyncio
async def test_set_handoff_uses_wrapper_session_without_nested_session_id(
    resolving_tool_proxy: tuple[ToolProxyService, MagicMock, MagicMock],
) -> None:
    proxy, mcp_manager, _ = resolving_tool_proxy
    _use_schema(proxy, COMPACT_SELF_SCHEMA)

    result = await proxy.call_tool(
        "gobby-sessions",
        "set_handoff",
        arguments={"rule_name": "build-coordinator-handoff"},
        session_id="#7",
        enforce_workflow=False,
    )

    assert result["success"] is True
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-sessions",
        tool_name="set_handoff",
        arguments={"rule_name": "build-coordinator-handoff"},
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
        tool_name="get_skill",
        arguments={"name": "brevity", "session_id": SESSION_UUID_3},
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
        tool_name="get_skill",
        arguments={"name": "brevity", "session_id": SESSION_UUID_3},
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
        tool_name="list_skills",
        arguments={},
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
        tool_name="list_skills",
        arguments={},
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
        tool_name="get_session",
        arguments={"session_id": SESSION_UUID_7},
        session_id=SESSION_UUID_3,
    )


@pytest.mark.asyncio
async def test_required_project_id_uses_ambient_project_context(
    tool_proxy: tuple[ToolProxyService, MagicMock],
) -> None:
    proxy, mcp_manager = tool_proxy
    _use_schema(proxy, PROJECT_ID_SCHEMA)

    token = set_project_context({"id": "target-project-id"})
    try:
        result = await proxy.call_tool(
            "gobby-tasks-ops",
            "run_expansion_qa_coverage",
            arguments={"run_id": "expand-123"},
            enforce_workflow=False,
        )
    finally:
        reset_project_context(token)

    assert result["success"] is True
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-tasks-ops",
        tool_name="run_expansion_qa_coverage",
        arguments={"run_id": "expand-123", "project_id": "target-project-id"},
        session_id=None,
    )


@pytest.mark.asyncio
async def test_required_project_id_uses_ambient_project_context_for_empty_arguments(
    tool_proxy: tuple[ToolProxyService, MagicMock],
) -> None:
    proxy, mcp_manager = tool_proxy
    _use_schema(proxy, PROJECT_ID_SCHEMA)

    token = set_project_context({"id": "empty-target-project-id"})
    try:
        result = await proxy.call_tool(
            "gobby-tasks-ops",
            "run_expansion_qa_coverage",
            arguments={},
            enforce_workflow=False,
        )
    finally:
        reset_project_context(token)

    assert result["success"] is True
    mcp_manager.call_tool.assert_awaited_once_with(
        "gobby-tasks-ops",
        tool_name="run_expansion_qa_coverage",
        arguments={"project_id": "empty-target-project-id"},
        session_id=None,
    )
