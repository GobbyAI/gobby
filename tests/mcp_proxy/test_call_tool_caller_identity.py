"""/mcp proxy tools that take a caller session_id must name the authenticated caller."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from mcp.types import CallToolResult, TextContent

from gobby.mcp_proxy.server import GobbyDaemonTools
from gobby.utils.local_token import AgentApiTokenClaims
from gobby.utils.session_context import reset_daemon_execution, set_daemon_execution
from tests.mcp_proxy.caller_principal_support import (
    FIXTURE_PROJECT_ID,
    bound_principal,
    session_claims,
    session_ref_resolves_to,
)

pytestmark = pytest.mark.unit

PROJECT_ID = FIXTURE_PROJECT_ID
CALLER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SUCCESSOR = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _claims(*, agent_run_id: str | None = None) -> AgentApiTokenClaims:
    return session_claims(CALLER, project_id=PROJECT_ID, agent_run_id=agent_run_id)


@pytest.fixture
def tools() -> Iterator[GobbyDaemonTools]:
    handler = GobbyDaemonTools(
        mcp_manager=MagicMock(),
        daemon_port=8787,
        websocket_port=8788,
        start_time=1000.0,
        internal_manager=MagicMock(),
        db=MagicMock(),
        session_manager=MagicMock(),
    )
    proxy = AsyncMock(return_value={"success": True, "result": "ran"})
    with patch.object(handler.tool_proxy, "call_tool", proxy):
        yield handler


async def _call(tools: GobbyDaemonTools, session_ref: str | None) -> Any:
    return await tools.call_tool(
        server_name="gobby-tasks", tool_name="claim_task", session_id=session_ref
    )


def _proxied_session(tools: GobbyDaemonTools) -> str | None:
    proxy = tools.tool_proxy.call_tool
    assert isinstance(proxy, AsyncMock)
    proxy.assert_awaited_once()
    assert proxy.await_args is not None
    session: str | None = proxy.await_args.args[3]
    return session


def _assert_refused(tools: GobbyDaemonTools, result: Any, session_ref: str) -> None:
    assert isinstance(result, CallToolResult)
    assert result.is_error
    content = result.content[0]
    assert isinstance(content, TextContent)
    assert f"session_id '{session_ref}'" in content.text
    assert "authenticated caller" in content.text
    proxy = tools.tool_proxy.call_tool
    assert isinstance(proxy, AsyncMock)
    proxy.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_caller_naming_another_session_is_refused(tools: GobbyDaemonTools) -> None:
    with bound_principal(_claims()), session_ref_resolves_to(OTHER):
        result = await _call(tools, "#42")

    _assert_refused(tools, result, "#42")


@pytest.mark.asyncio
async def test_unseeded_principal_with_session_id_fails_closed(tools: GobbyDaemonTools) -> None:
    with session_ref_resolves_to(OTHER):
        result = await _call(tools, OTHER)

    _assert_refused(tools, result, OTHER)


@pytest.mark.asyncio
async def test_rejected_principal_with_session_id_is_refused(tools: GobbyDaemonTools) -> None:
    rejected: Literal[False] = False
    with bound_principal(rejected), session_ref_resolves_to(OTHER):
        result = await _call(tools, OTHER)

    _assert_refused(tools, result, OTHER)


@pytest.mark.asyncio
@pytest.mark.parametrize("session_ref", ["#7", CALLER, CALLER[:8]])
async def test_managed_caller_naming_its_own_session_runs(
    tools: GobbyDaemonTools, session_ref: str
) -> None:
    with bound_principal(_claims()), session_ref_resolves_to(CALLER):
        result = await _call(tools, session_ref)

    assert result == {"result": "ran"}
    assert _proxied_session(tools) == CALLER


@pytest.mark.asyncio
async def test_cleared_run_successor_counts_as_the_caller(tools: GobbyDaemonTools) -> None:
    with (
        bound_principal(_claims(agent_run_id="run-1")),
        session_ref_resolves_to(SUCCESSOR),
        patch("gobby.mcp_proxy.server.current_run_session_id", return_value=SUCCESSOR) as live,
    ):
        result = await _call(tools, "#9")

    assert result == {"result": "ran"}
    assert _proxied_session(tools) == SUCCESSOR
    assert live.call_args.kwargs == {
        "agent_run_id": "run-1",
        "session_id": CALLER,
        "project_id": PROJECT_ID,
    }


@pytest.mark.asyncio
async def test_operator_may_delegate_any_session(tools: GobbyDaemonTools) -> None:
    with bound_principal(None), session_ref_resolves_to(OTHER):
        result = await _call(tools, "#42")

    assert result == {"result": "ran"}
    assert _proxied_session(tools) == OTHER


@pytest.mark.asyncio
async def test_daemon_execution_may_delegate_any_session(tools: GobbyDaemonTools) -> None:
    token = set_daemon_execution()
    try:
        with session_ref_resolves_to(OTHER):
            result = await _call(tools, "#42")
    finally:
        reset_daemon_execution(token)

    assert result == {"result": "ran"}
    assert _proxied_session(tools) == OTHER


@pytest.mark.asyncio
async def test_call_without_session_id_needs_no_principal(tools: GobbyDaemonTools) -> None:
    result = await _call(tools, None)

    assert result == {"result": "ran"}
    assert _proxied_session(tools) is None


# --- The other /mcp proxy tools that take a caller session_id share the gate. ---


class _Tool:
    """How one session-taking /mcp tool is invoked and where its session lands."""

    def __init__(self, name: str, target: str, session_arg: int | str) -> None:
        self.name = name
        self.target = target
        self.session_arg = session_arg
        self.downstream: Mock = Mock()

    @contextmanager
    def patched(self, tools: GobbyDaemonTools) -> Iterator["_Tool"]:
        with ExitStack() as stack:
            if self.name == "get_tool_schema":
                schema = {"success": True, "tool": {"name": "claim_task"}}
                stack.enter_context(
                    patch.object(
                        tools.tool_proxy, "get_tool_schema", AsyncMock(return_value=schema)
                    )
                )
            if self.name == "list_mcp_servers":
                servers = {"success": True, "servers": []}
                stack.enter_context(
                    patch.object(tools.tool_proxy, "list_servers", AsyncMock(return_value=servers))
                )
            mock: Mock
            if self.target == "tool_proxy.list_tools":
                mock = AsyncMock(return_value={"success": True, "tools": []})
                stack.enter_context(patch.object(tools.tool_proxy, "list_tools", mock))
            elif self.target == "tool_proxy.record_servers_listed":
                mock = MagicMock()
                stack.enter_context(patch.object(tools.tool_proxy, "record_servers_listed", mock))
            else:
                mock = stack.enter_context(
                    patch(self.target, return_value={"success": True, "value": 1})
                )
            self.downstream = mock
            yield self

    async def invoke(self, tools: GobbyDaemonTools, session_ref: str) -> Any:
        if self.name == "list_tools":
            return await tools.list_tools("gobby-tasks", session_id=session_ref)
        if self.name == "get_tool_schema":
            return await tools.get_tool_schema("gobby-tasks", "claim_task", session_id=session_ref)
        if self.name == "list_mcp_servers":
            return await tools.list_mcp_servers(session_id=session_ref)
        if self.name == "set_variable":
            return await tools.set_variable("flag", True, session_id=session_ref)
        return await tools.get_variable("flag", session_id=session_ref)

    def received_session(self) -> str | None:
        self.downstream.assert_called_once()
        call = self.downstream.call_args
        session: str | None = (
            call.args[self.session_arg]
            if isinstance(self.session_arg, int)
            else call.kwargs[self.session_arg]
        )
        return session


def _session_tools() -> list[Any]:
    variables = "gobby.mcp_proxy.tools.workflows._variables"
    return [
        pytest.param(_Tool("list_tools", "tool_proxy.list_tools", "session_id"), id="list_tools"),
        pytest.param(
            _Tool("get_tool_schema", "gobby.mcp_proxy.server.record_schema_shown", 1),
            id="get_tool_schema",
        ),
        pytest.param(_Tool("set_variable", f"{variables}.set_variable", 4), id="set_variable"),
        pytest.param(_Tool("get_variable", f"{variables}.get_variable", 3), id="get_variable"),
        pytest.param(
            _Tool("list_mcp_servers", "tool_proxy.record_servers_listed", 0),
            id="list_mcp_servers",
        ),
    ]


def _assert_tool_refused(tool: _Tool, result: Any, session_ref: str) -> None:
    assert isinstance(result, dict)
    assert result["success"] is False
    assert f"session_id '{session_ref}'" in result["error"]
    assert "authenticated caller" in result["error"]
    tool.downstream.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _session_tools())
async def test_tool_refuses_managed_caller_naming_another_session(
    tools: GobbyDaemonTools, tool: _Tool
) -> None:
    with tool.patched(tools), bound_principal(_claims()), session_ref_resolves_to(OTHER):
        result = await tool.invoke(tools, "#42")

    _assert_tool_refused(tool, result, "#42")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _session_tools())
async def test_tool_with_unseeded_principal_fails_closed(
    tools: GobbyDaemonTools, tool: _Tool
) -> None:
    with tool.patched(tools), session_ref_resolves_to(OTHER):
        result = await tool.invoke(tools, OTHER)

    _assert_tool_refused(tool, result, OTHER)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _session_tools())
async def test_tool_refuses_rejected_principal(tools: GobbyDaemonTools, tool: _Tool) -> None:
    rejected: Literal[False] = False
    with tool.patched(tools), bound_principal(rejected), session_ref_resolves_to(OTHER):
        result = await tool.invoke(tools, OTHER)

    _assert_tool_refused(tool, result, OTHER)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _session_tools())
async def test_tool_refuses_unresolvable_session_id(tools: GobbyDaemonTools, tool: _Tool) -> None:
    unresolved = (None, None, None, None)
    with (
        tool.patched(tools),
        bound_principal(None),
        patch("gobby.utils.session_context._resolve_context_values", return_value=unresolved),
    ):
        result = await tool.invoke(tools, "#404")

    assert isinstance(result, dict)
    assert result["success"] is False
    assert "session_id '#404' not found" in result["error"]
    tool.downstream.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _session_tools())
async def test_tool_runs_for_callers_own_session_as_resolved_uuid(
    tools: GobbyDaemonTools, tool: _Tool
) -> None:
    with tool.patched(tools), bound_principal(_claims()), session_ref_resolves_to(CALLER):
        result = await tool.invoke(tools, "#7")

    assert result["success"] is True
    assert tool.received_session() == CALLER


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _session_tools())
async def test_tool_runs_for_cleared_run_successor(tools: GobbyDaemonTools, tool: _Tool) -> None:
    with (
        tool.patched(tools),
        bound_principal(_claims(agent_run_id="run-1")),
        session_ref_resolves_to(SUCCESSOR),
        patch("gobby.mcp_proxy.server.current_run_session_id", return_value=SUCCESSOR),
    ):
        result = await tool.invoke(tools, "#9")

    assert result["success"] is True
    assert tool.received_session() == SUCCESSOR


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _session_tools())
async def test_tool_lets_operator_delegate_any_session(
    tools: GobbyDaemonTools, tool: _Tool
) -> None:
    with tool.patched(tools), bound_principal(None), session_ref_resolves_to(OTHER):
        result = await tool.invoke(tools, "#42")

    assert result["success"] is True
    assert tool.received_session() == OTHER


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _session_tools())
async def test_tool_lets_daemon_execution_delegate_any_session(
    tools: GobbyDaemonTools, tool: _Tool
) -> None:
    token = set_daemon_execution()
    try:
        with tool.patched(tools), session_ref_resolves_to(OTHER):
            result = await tool.invoke(tools, "#42")
    finally:
        reset_daemon_execution(token)

    assert result["success"] is True
    assert tool.received_session() == OTHER
