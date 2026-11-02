"""Protocol schema delivery must grant the same lease as hooked/HTTP delivery."""

from types import SimpleNamespace
from typing import Any
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
    assert not variables.get_variables(other.id).get("unlocked_tools")
    assert variables.get_variables(caller.id).get("unlocked_tools", []) == (
        ["gobby-tasks:get_task"] if authenticated else []
    )


@pytest.mark.asyncio
async def test_hookless_schema_lookup_admits_actual_call(
    session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    """Protocol delivery unlocks the real before-tool rule without post-tool hooks."""
    import asyncio

    from gobby.hooks.events import HookEvent, HookResponse
    from gobby.workflows.engine.core import RuleEngine
    from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

    db = session_manager.db
    sync_bundled_rules(db, get_bundled_rules_path())
    db.execute("UPDATE rule_definitions SET source = 'installed', enabled = FALSE")
    db.execute(
        "UPDATE rule_definitions SET enabled = TRUE WHERE name = %s",
        ("require-current-context-schema-before-call",),
    )
    session = session_manager.register(
        external_id="hookless-admission",
        machine_id=require_machine_id(),
        source="grok",
        project_id=sample_project["id"],
    )
    variables = SessionVariableManager(db)
    variables.set_variable(session.id, "enforce_tool_schema_check", True)
    engine = RuleEngine(db)

    def evaluate(event: HookEvent) -> HookResponse:
        result = asyncio.run(
            engine.evaluate(event, session.id, variables.get_variables(session.id))
        )
        return HookResponse(decision=result.decision, reason=result.reason)

    hook_manager = SimpleNamespace(
        _database=db,
        _session_manager=session_manager,
        _workflow_handler=SimpleNamespace(evaluate=evaluate),
    )
    internal = MagicMock()
    internal.is_internal.return_value = True
    registry = internal.get_registry.return_value
    registry.call = AsyncMock(return_value={"success": True, "delivered": True})
    tools = GobbyDaemonTools(
        mcp_manager=MagicMock(),
        daemon_port=8787,
        websocket_port=8788,
        start_time=0,
        internal_manager=internal,
        db=db,
        session_manager=session_manager,
        hook_manager_resolver=lambda: hook_manager,
    )
    schema = {"success": True, "tool": {"name": "get_task", "inputSchema": {"type": "object"}}}
    with (
        patch.object(tools.tool_proxy, "get_tool_schema", AsyncMock(return_value=schema)),
        session_context_for_test(session.id),
    ):
        blocked = await tools.call_tool("gobby-tasks", "get_task", {})
        assert blocked.is_error is True
        registry.call.assert_not_awaited()
        await tools.get_tool_schema("gobby-tasks", "get_task")
        admitted = await tools.call_tool("gobby-tasks", "get_task", {})
    assert admitted == {"delivered": True}
    registry.call.assert_awaited_once()
