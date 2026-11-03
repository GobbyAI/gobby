"""The /mcp call_tool wrapper session_id must name the authenticated caller."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult, TextContent

from gobby.mcp_proxy.server import GobbyDaemonTools
from gobby.utils.local_token import AgentApiTokenClaims
from gobby.utils.session_context import (
    RequestPrincipal,
    SessionContext,
    reset_daemon_execution,
    reset_request_principal,
    set_daemon_execution,
    set_request_principal,
)

pytestmark = pytest.mark.unit

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
CALLER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SUCCESSOR = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _claims(*, agent_run_id: str | None = None) -> AgentApiTokenClaims:
    return AgentApiTokenClaims(
        session_id=CALLER,
        project_id=PROJECT_ID,
        machine_id="local-machine",
        iat=1_700_000_000,
        exp=1_700_003_600,
        agent_run_id=agent_run_id,
    )


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


@contextmanager
def _principal(principal: RequestPrincipal) -> Iterator[None]:
    async def resolve() -> RequestPrincipal:
        return principal

    token = set_request_principal(resolve)
    try:
        yield
    finally:
        reset_request_principal(token)


@contextmanager
def _session_ref_resolves_to(session_id: str) -> Iterator[None]:
    resolved = (session_id, PROJECT_ID, SessionContext(session_id=session_id), {"id": PROJECT_ID})
    with patch("gobby.utils.session_context._resolve_context_values", return_value=resolved):
        yield


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
    with _principal(_claims()), _session_ref_resolves_to(OTHER):
        result = await _call(tools, "#42")

    _assert_refused(tools, result, "#42")


@pytest.mark.asyncio
async def test_unseeded_principal_with_session_id_fails_closed(tools: GobbyDaemonTools) -> None:
    with _session_ref_resolves_to(OTHER):
        result = await _call(tools, OTHER)

    _assert_refused(tools, result, OTHER)


@pytest.mark.asyncio
async def test_rejected_principal_with_session_id_is_refused(tools: GobbyDaemonTools) -> None:
    rejected: Literal[False] = False
    with _principal(rejected), _session_ref_resolves_to(OTHER):
        result = await _call(tools, OTHER)

    _assert_refused(tools, result, OTHER)


@pytest.mark.asyncio
@pytest.mark.parametrize("session_ref", ["#7", CALLER, CALLER[:8]])
async def test_managed_caller_naming_its_own_session_runs(
    tools: GobbyDaemonTools, session_ref: str
) -> None:
    with _principal(_claims()), _session_ref_resolves_to(CALLER):
        result = await _call(tools, session_ref)

    assert result == {"result": "ran"}
    assert _proxied_session(tools) == CALLER


@pytest.mark.asyncio
async def test_cleared_run_successor_counts_as_the_caller(tools: GobbyDaemonTools) -> None:
    with (
        _principal(_claims(agent_run_id="run-1")),
        _session_ref_resolves_to(SUCCESSOR),
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
    with _principal(None), _session_ref_resolves_to(OTHER):
        result = await _call(tools, "#42")

    assert result == {"result": "ran"}
    assert _proxied_session(tools) == OTHER


@pytest.mark.asyncio
async def test_daemon_execution_may_delegate_any_session(tools: GobbyDaemonTools) -> None:
    token = set_daemon_execution()
    try:
        with _session_ref_resolves_to(OTHER):
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
