"""Protocol schema delivery must grant the same lease as hooked/HTTP delivery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.server import GobbyDaemonTools
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
@pytest.mark.parametrize("explicit_session", [False, True])
async def test_protocol_schema_lease(
    session_manager: SessionManager, success: bool, explicit_session: bool
) -> None:
    session = session_manager.register(
        external_id="hookless-schema",
        machine_id=require_machine_id(),
        source="grok",
        project_id=None,
    )
    tools = GobbyDaemonTools(
        mcp_manager=MagicMock(),
        daemon_port=8787,
        websocket_port=8788,
        start_time=0,
        internal_manager=MagicMock(),
        db=session_manager.db,
        session_manager=session_manager,
        hook_manager_resolver=lambda: SimpleNamespace(_database=session_manager.db),
    )
    response = {"success": success, "tool": {"name": "get_task", "inputSchema": {}}}
    with (
        patch.object(tools.tool_proxy, "get_tool_schema", AsyncMock(return_value=response)),
        session_context_for_test(session.id),
    ):
        for _ in range(2):
            result = await tools.get_tool_schema(
                "gobby-tasks", "get_task", session_id=session.id if explicit_session else None
            )
            assert result == response
    variables = SessionVariableManager(session_manager.db).get_variables(session.id)
    assert variables.get("unlocked_tools", []) == (["gobby-tasks:get_task"] if success else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("authenticated", [True, False])
async def test_schema_lease_cannot_target_another_session(
    session_manager: SessionManager, authenticated: bool
) -> None:
    from contextlib import nullcontext

    caller, other = [
        session_manager.register(
            external_id=f"lease-{name}",
            machine_id=require_machine_id(),
            source="grok",
            project_id=None,
        )
        for name in ("caller", "other")
    ]
    tools = GobbyDaemonTools(
        mcp_manager=MagicMock(),
        daemon_port=8787,
        websocket_port=8788,
        start_time=0,
        internal_manager=MagicMock(),
        db=session_manager.db,
        session_manager=session_manager,
        hook_manager_resolver=lambda: SimpleNamespace(_database=session_manager.db),
    )
    response = {"success": True, "tool": {"name": "get_task", "inputSchema": {}}}
    with (
        patch.object(tools.tool_proxy, "get_tool_schema", AsyncMock(return_value=response)),
        session_context_for_test(caller.id) if authenticated else nullcontext(),
    ):
        await tools.get_tool_schema("gobby-tasks", "get_task", session_id=other.id)
    variables = SessionVariableManager(session_manager.db)
    assert variables.get_variables(other.id).get("unlocked_tools", []) == ["gobby-tasks:get_task"]
    assert not variables.get_variables(caller.id).get("unlocked_tools")
