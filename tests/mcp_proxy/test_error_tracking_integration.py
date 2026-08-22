from __future__ import annotations

import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult

from gobby.hooks.events import HookResponse
from gobby.mcp_proxy.models import ToolProxyErrorCode
from gobby.mcp_proxy.server import GobbyDaemonTools
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _proxy(
    *,
    result: object,
    workflow_handler: MagicMock | None = None,
) -> tuple[ToolProxyService, MagicMock, MagicMock]:
    mcp_manager = MagicMock()
    mcp_manager.project_id = "project-1"
    mcp_manager.has_server.return_value = True
    mcp_manager.call_tool = AsyncMock(return_value=result)
    mcp_manager.get_tool_schema = AsyncMock(return_value={"success": False})

    internal_manager = MagicMock()
    internal_manager.is_internal.return_value = False

    session_manager = MagicMock()
    session_manager.resolve_session_reference.side_effect = (
        lambda session_id, project_id=None: session_id
    )
    session_manager.get.return_value = SimpleNamespace(
        source="claude",
        session_type="terminal",
        project_id="project-1",
        external_id="external-1",
    )
    hook_manager = MagicMock()
    hook_manager._workflow_handler = workflow_handler
    hook_manager._session_manager = session_manager
    hook_manager._database = MagicMock()

    proxy = ToolProxyService(
        mcp_manager=mcp_manager,
        internal_manager=internal_manager,
        validate_arguments=False,
        hook_manager_resolver=lambda: hook_manager,
    )
    return proxy, mcp_manager, internal_manager


def _persisting_proxy(
    temp_db: HubDatabase,
    *,
    result: object,
    workflow_handler: MagicMock | None = None,
) -> tuple[ToolProxyService, MagicMock, str]:
    project_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    temp_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        (project_id, "error-tracking"),
    )
    session_manager = SessionManager(temp_db)
    session_id = session_manager.register_session(
        external_id="error-tracking-session",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=project_id,
        project_path="/tmp/error-tracking",
    )
    assert session_id

    mcp_manager = MagicMock()
    mcp_manager.project_id = project_id
    mcp_manager.session_manager = session_manager
    mcp_manager.has_server.return_value = True
    mcp_manager.call_tool = AsyncMock(return_value=result)
    internal_manager = MagicMock()
    internal_manager.is_internal.return_value = False
    hook_manager = MagicMock()
    hook_manager._workflow_handler = workflow_handler
    hook_manager._session_manager = session_manager
    hook_manager._database = temp_db
    hook_manager._message_processor.reconcile_codex_transcript = AsyncMock(
        return_value=SimpleNamespace(flushed=True, error=None)
    )
    proxy = ToolProxyService(
        mcp_manager=mcp_manager,
        internal_manager=internal_manager,
        validate_arguments=False,
        hook_manager_resolver=lambda: hook_manager,
    )
    return proxy, mcp_manager, session_id


@pytest.mark.asyncio
async def test_executed_failure_finalizes_once_off_event_loop_thread() -> None:
    from gobby.mcp_proxy.services import tool_execution

    proxy, _, _ = _proxy(result={"success": False, "error": "boom"})
    loop_thread = threading.get_ident()
    tracking_threads: list[int] = []
    identity_threads: list[int] = []
    identity_arguments = tool_execution._identity_arguments

    def capture_tracking(*_args: object) -> None:
        tracking_threads.append(threading.get_ident())

    def capture_identity(arguments: Any) -> dict[str, Any]:
        identity_threads.append(threading.get_ident())
        return identity_arguments(arguments)

    with (
        patch(
            "gobby.mcp_proxy.services.tool_execution.track_proxy_outcome",
            side_effect=capture_tracking,
        ) as tracking,
        patch(
            "gobby.mcp_proxy.services.tool_execution._identity_arguments",
            side_effect=capture_identity,
        ),
    ):
        result = await proxy.call_tool(
            "server-a",
            "run",
            {"command": "false"},
            session_id="session-1",
        )

    assert result == {"success": False, "error": "boom"}
    assert tracking.call_count == 1
    assert tracking_threads and tracking_threads[0] != loop_thread
    assert identity_threads and all(thread_id != loop_thread for thread_id in identity_threads)
    assert tracking.call_args.args[1:] == (
        "session-1",
        ("server-a", "run", {"command": "false"}),
        ("server-a", "run", {"command": "false"}),
        result,
        "executed",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_result", "handler_error", "expected_class"),
    [
        (HookResponse(decision="block", reason="denied"), None, "policy_denied"),
        (None, RuntimeError("workflow unavailable"), "failed_pre_dispatch"),
    ],
)
async def test_before_tool_return_sites_carry_structural_outcome_class(
    handler_result: HookResponse | None,
    handler_error: Exception | None,
    expected_class: str,
) -> None:
    workflow_handler = MagicMock()
    if handler_error is not None:
        workflow_handler.evaluate.side_effect = handler_error
    else:
        workflow_handler.evaluate.return_value = handler_result
    proxy, mcp_manager, _ = _proxy(result={"success": True}, workflow_handler=workflow_handler)

    with (
        patch("gobby.mcp_proxy.services.tool_execution.track_proxy_outcome") as tracking,
        patch(
            "gobby.mcp_proxy.services.result_handling.audit_source_block",
            new_callable=AsyncMock,
        ) as audit_source_block,
    ):
        result = await proxy.call_tool(
            "server-a",
            "run",
            {"command": "echo ok"},
            session_id="session-1",
        )

    assert result["success"] is False
    assert tracking.call_args.args[-1] == expected_class
    mcp_manager.call_tool.assert_not_awaited()
    if handler_error is not None:
        audit_source_block.assert_awaited_once()
    else:
        audit_source_block.assert_not_awaited()


@pytest.mark.asyncio
async def test_rule_rewrite_preserves_caller_and_final_dispatch_identities() -> None:
    workflow_handler = MagicMock()
    workflow_handler.evaluate.return_value = HookResponse(
        decision="allow",
        modified_input={
            "server_name": "server-b",
            "tool_name": "fixed",
            "arguments": {"command": "echo fixed"},
        },
    )
    proxy, mcp_manager, _ = _proxy(result={"success": True}, workflow_handler=workflow_handler)

    with patch("gobby.mcp_proxy.services.tool_execution.track_proxy_outcome") as tracking:
        await proxy.call_tool(
            "server-a",
            "run",
            {"command": "echo original"},
            session_id="session-1",
        )

    assert tracking.call_args.args[2:4] == (
        ("server-a", "run", {"command": "echo original"}),
        ("server-b", "fixed", {"command": "echo fixed"}),
    )
    mcp_manager.call_tool.assert_awaited_once_with(
        "server-b",
        "fixed",
        {"command": "echo fixed"},
        session_id="session-1",
    )


@pytest.mark.asyncio
async def test_proxy_namespace_recursion_finalizes_exactly_once() -> None:
    proxy, _, _ = _proxy(result={"success": False, "error": "nested failure"})

    with (
        patch.object(proxy, "_is_proxy_namespace", side_effect=lambda name: name == "gobby"),
        patch.object(proxy, "_resolve_server_for_tool", return_value="server-a"),
        patch("gobby.mcp_proxy.services.tool_execution.track_proxy_outcome") as tracking,
    ):
        await proxy.call_tool(
            "gobby",
            "run",
            {"command": "false"},
            session_id="session-1",
        )

    assert tracking.call_count == 1
    assert tracking.call_args.args[2:4] == (
        ("gobby", "run", {"command": "false"}),
        ("server-a", "run", {"command": "false"}),
    )


@pytest.mark.asyncio
async def test_internal_registry_failure_is_tracked_once_as_executed() -> None:
    proxy, mcp_manager, internal_manager = _proxy(result={"success": True})
    registry = MagicMock()
    registry.call = AsyncMock(return_value={"success": False, "error": "pipeline failed"})
    internal_manager.is_internal.return_value = True
    internal_manager.get_registry.return_value = registry

    with patch("gobby.mcp_proxy.services.tool_execution.track_proxy_outcome") as tracking:
        result = await proxy.call_tool(
            "gobby-pipelines",
            "run",
            {"pipeline": "build"},
            session_id="session-1",
        )

    assert result == {"success": False, "error": "pipeline failed"}
    assert tracking.call_count == 1
    assert tracking.call_args.args[-1] == "executed"
    registry.call.assert_awaited_once_with("run", {"pipeline": "build"})
    mcp_manager.call_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server_name", "internal"),
    [
        ("missing-external", False),
        ("gobby-missing", True),
    ],
)
async def test_unknown_named_server_is_invalid_call(
    server_name: str,
    internal: bool,
) -> None:
    proxy, mcp_manager, internal_manager = _proxy(result={"success": True})
    internal_manager.is_internal.return_value = internal
    internal_manager.get_registry.return_value = None
    mcp_manager.has_server.return_value = False

    with patch("gobby.mcp_proxy.services.tool_execution.track_proxy_outcome") as tracking:
        result = await proxy.call_tool(
            server_name,
            "run",
            {},
            session_id="session-1",
        )

    assert result["success"] is False
    assert result["error_code"] == ToolProxyErrorCode.SERVER_NOT_FOUND.value
    assert tracking.call_args.args[-1] == "invalid_call"


@pytest.mark.asyncio
async def test_malformed_caller_arguments_are_invalid_and_never_dispatch() -> None:
    proxy, mcp_manager, _ = _proxy(result={"success": True})

    with patch("gobby.mcp_proxy.services.tool_execution.track_proxy_outcome") as tracking:
        result = await proxy.call_tool(
            "server-a",
            "run",
            "{not-json",
            session_id="session-1",
        )

    assert result["success"] is False
    assert tracking.call_args.args[-1] == "invalid_call"
    mcp_manager.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_schema_validation_failure_is_invalid_and_never_dispatches() -> None:
    proxy, mcp_manager, _ = _proxy(result={"success": True})
    proxy._validate_arguments = True
    schema_result = {
        "success": True,
        "tool": {
            "inputSchema": {
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
            }
        },
    }

    with (
        patch.object(
            proxy,
            "get_tool_schema",
            new=AsyncMock(return_value=schema_result),
        ),
        patch("gobby.mcp_proxy.services.tool_execution.track_proxy_outcome") as tracking,
    ):
        result = await proxy.call_tool(
            "server-a",
            "run",
            {"count": "wrong"},
            session_id="session-1",
        )

    assert result["success"] is False
    assert tracking.call_args.args[-1] == "invalid_call"
    mcp_manager.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_unresolved_proxy_namespace_is_invalid_call() -> None:
    proxy, mcp_manager, _ = _proxy(result={"success": True})

    with (
        patch.object(proxy, "_is_proxy_namespace", return_value=True),
        patch.object(proxy, "_resolve_server_for_tool", return_value=None),
        patch("gobby.mcp_proxy.services.tool_execution.track_proxy_outcome") as tracking,
    ):
        result = await proxy.call_tool(
            "gobby",
            "missing_tool",
            {},
            session_id="session-1",
        )

    assert result["success"] is False
    assert tracking.call_args.args[-1] == "invalid_call"
    mcp_manager.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_tool_filter_denial_is_policy_denied() -> None:
    proxy, mcp_manager, _ = _proxy(result={"success": True})
    proxy._tool_filter = MagicMock()
    proxy._tool_filter.is_tool_allowed.return_value = (False, "filtered")

    with patch("gobby.mcp_proxy.services.tool_execution.track_proxy_outcome") as tracking:
        result = await proxy.call_tool(
            "server-a",
            "run",
            {},
            session_id="session-1",
        )

    assert result["success"] is False
    assert tracking.call_args.args[-1] == "policy_denied"
    mcp_manager.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_rule_rewrite_is_failed_pre_dispatch() -> None:
    workflow_handler = MagicMock()
    workflow_handler.evaluate.return_value = HookResponse(
        decision="allow",
        modified_input={"arguments": "{not-json"},
    )
    proxy, mcp_manager, _ = _proxy(result={"success": True}, workflow_handler=workflow_handler)

    with patch("gobby.mcp_proxy.services.tool_execution.track_proxy_outcome") as tracking:
        result = await proxy.call_tool(
            "server-a",
            "run",
            {},
            session_id="session-1",
        )

    assert result["success"] is False
    assert tracking.call_args.args[-1] == "failed_pre_dispatch"
    mcp_manager.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_after_tool_evaluation_failure_keeps_executed_ownership() -> None:
    workflow_handler = MagicMock()
    workflow_handler.evaluate.side_effect = [
        HookResponse(decision="allow"),
        RuntimeError("after-tool unavailable"),
    ]
    proxy, mcp_manager, _ = _proxy(
        result={"success": False, "error": "dispatch failed"},
        workflow_handler=workflow_handler,
    )

    with patch("gobby.mcp_proxy.services.tool_execution.track_proxy_outcome") as tracking:
        result = await proxy.call_tool(
            "server-a",
            "run",
            {},
            session_id="session-1",
        )

    assert result == {"success": False, "error": "dispatch failed"}
    assert tracking.call_args.args[-1] == "executed"
    mcp_manager.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_proxy_failure_persists_and_matching_success_resolves(
    temp_db: HubDatabase,
) -> None:
    proxy, mcp_manager, session_id = _persisting_proxy(
        temp_db,
        result={"success": False, "error": "task is blocked"},
    )

    await proxy.call_tool(
        "gobby-tasks",
        "close_task",
        {"task_id": "#18819"},
        session_id=session_id,
    )

    records = SessionVariableManager(temp_db).get_variables(session_id)["open_tool_errors"]
    assert len(records) == 1
    assert records[0]["tool"] == "gobby-tasks/close_task"
    assert records[0]["error"] == "task is blocked"

    mcp_manager.call_tool.return_value = {"success": True, "ref": "#18819"}
    await proxy.call_tool(
        "gobby-tasks",
        "close_task",
        {"task_id": "#18819"},
        session_id=session_id,
    )

    assert SessionVariableManager(temp_db).get_variables(session_id)["open_tool_errors"] == []


@pytest.mark.asyncio
async def test_rewritten_executed_failure_replaces_caller_predispatch_record(
    temp_db: HubDatabase,
) -> None:
    workflow_handler = MagicMock()
    workflow_handler.evaluate.side_effect = [
        RuntimeError("workflow unavailable"),
        HookResponse(
            decision="allow",
            modified_input={
                "server_name": "server-b",
                "tool_name": "fixed",
                "arguments": {"command": "echo fixed"},
            },
        ),
    ]
    proxy, _, session_id = _persisting_proxy(
        temp_db,
        result={"success": False, "error": "dispatch failed"},
        workflow_handler=workflow_handler,
    )

    await proxy.call_tool(
        "server-a",
        "run",
        {"command": "echo original"},
        session_id=session_id,
    )
    await proxy.call_tool(
        "server-a",
        "run",
        {"command": "echo original"},
        session_id=session_id,
    )

    records = SessionVariableManager(temp_db).get_variables(session_id)["open_tool_errors"]
    assert len(records) == 1
    assert records[0]["tool"] == "server-b/fixed"
    assert records[0]["error"] == "dispatch failed"
    assert records[0]["count"] == 1


@pytest.mark.asyncio
async def test_same_identity_failure_after_predispatch_increments_standing_record(
    temp_db: HubDatabase,
) -> None:
    workflow_handler = MagicMock()
    workflow_handler.evaluate.side_effect = [
        RuntimeError("workflow unavailable"),
        HookResponse(decision="allow"),
    ]
    proxy, _, session_id = _persisting_proxy(
        temp_db,
        result={"success": False, "error": "dispatch failed"},
        workflow_handler=workflow_handler,
    )

    for _ in range(2):
        await proxy.call_tool(
            "server-a",
            "run",
            {"command": "echo original"},
            session_id=session_id,
        )

    records = SessionVariableManager(temp_db).get_variables(session_id)["open_tool_errors"]
    assert len(records) == 1
    assert records[0]["tool"] == "server-a/run"
    assert records[0]["error"] == "dispatch failed"
    assert records[0]["count"] == 2


@pytest.mark.asyncio
async def test_daemon_wrapper_local_failures_do_not_track_and_delegated_failure_does(
    temp_db: HubDatabase,
) -> None:
    proxy, mcp_manager, session_id = _persisting_proxy(
        temp_db,
        result={"success": False, "error": "delegated failure"},
    )
    tools = GobbyDaemonTools(
        mcp_manager=mcp_manager,
        daemon_port=60887,
        websocket_port=60888,
        start_time=0.0,
        internal_manager=proxy._internal_manager,
        db=temp_db,
        session_manager=proxy.session_manager,
    )
    tools.tool_proxy = proxy

    with patch("gobby.mcp_proxy.services.tool_execution.track_proxy_outcome") as tracking:
        missing_route = await tools.call_tool()
        assert tracking.call_count == 0
        malformed = await tools.call_tool(arguments="{not-json")
        assert tracking.call_count == 0
        missing_project = await tools.call_tool(
            "server-a",
            "run",
            {},
            project_id="missing-project",
        )
        assert tracking.call_count == 0

        delegated = await tools.call_tool(
            "server-a",
            "run",
            {},
            session_id=session_id,
        )

    assert isinstance(missing_route, CallToolResult) and missing_route.is_error
    assert isinstance(malformed, CallToolResult) and malformed.is_error
    assert isinstance(missing_project, CallToolResult) and missing_project.is_error
    assert isinstance(delegated, CallToolResult) and delegated.is_error
    assert tracking.call_count == 1
