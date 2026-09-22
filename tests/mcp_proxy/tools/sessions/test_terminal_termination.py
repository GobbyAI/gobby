"""MCP terminal termination surface tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._terminal import register_terminal_tools
from gobby.storage.terminals import Terminal
from gobby.utils.session_context import session_context_for_test
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

pytestmark = pytest.mark.unit


def _terminate_tool(
    terminal: Terminal,
    sessions: MagicMock,
) -> tuple[Callable[..., Any], FakeRuntime]:
    registry = InternalToolRegistry(name="test", description="test")
    runtime = FakeRuntime()
    with patch(
        "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
        return_value=MagicMock(),
    ):
        register_terminal_tools(
            registry,
            sessions,
            MagicMock(fetchone=MagicMock(return_value=None)),
            terminal_manager=MemoryTerminalStore(terminal),
            terminal_runtime_registry=runtime_registry(runtime),
        )
    tool = registry.get_tool("terminate_terminal")
    assert tool is not None
    return tool, runtime


def test_terminate_terminal_kills_external_root_session_terminal() -> None:
    root_session_id = "root-session"
    terminal = make_memory_terminal(session_name="root-shell")
    terminal.ownership = "external"
    terminal.project_id = "project-1"
    terminal.session_id = root_session_id
    caller = SimpleNamespace(id="caller", project_id="project-1", agent_run_id=None)
    sessions = MagicMock()
    sessions.resolve_session_reference.side_effect = lambda reference, _project_id=None: reference
    sessions.get.return_value = caller
    tool, runtime = _terminate_tool(terminal, sessions)

    with (
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal_termination.get_request_principal",
            AsyncMock(return_value=None),
        ),
        session_context_for_test(caller.id),
    ):
        result = asyncio.run(tool(reference=root_session_id))

    assert result == {"success": True, "terminal_id": terminal.id, "state": "exited"}
    assert runtime.killed == ["root-shell"]
    assert sessions.resolve_session_reference.call_args_list == [
        call(caller.id),
        call(root_session_id, caller.project_id),
    ]


def test_terminate_terminal_kills_external_terminal_by_terminal_id() -> None:
    terminal = make_memory_terminal(session_name="external-shell")
    terminal.ownership = "external"
    terminal.project_id = "project-1"
    terminal.session_id = "root-session"
    caller = SimpleNamespace(id="caller", project_id="project-1", agent_run_id=None)
    sessions = MagicMock()
    sessions.get.return_value = caller
    tool, runtime = _terminate_tool(terminal, sessions)

    with (
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal_termination.get_request_principal",
            AsyncMock(return_value=None),
        ),
        session_context_for_test(caller.id),
    ):
        result = asyncio.run(tool(reference=terminal.id))

    assert result == {"success": True, "terminal_id": terminal.id, "state": "exited"}
    assert runtime.killed == ["external-shell"]
    sessions.resolve_session_reference.assert_called_once_with(caller.id)


def test_terminate_terminal_refuses_external_terminal_outside_caller_scope() -> None:
    root_session_id = "root-session"
    terminal = make_memory_terminal(session_name="foreign-shell")
    terminal.ownership = "external"
    terminal.project_id = "project-2"
    terminal.session_id = root_session_id
    caller = SimpleNamespace(id="caller", project_id="project-1", agent_run_id=None)
    sessions = MagicMock()
    sessions.resolve_session_reference.side_effect = lambda reference, _project_id=None: reference
    sessions.get.return_value = caller
    sessions.is_ancestor.return_value = False
    tool, runtime = _terminate_tool(terminal, sessions)

    with (
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal_termination.get_request_principal",
            AsyncMock(return_value=None),
        ),
        session_context_for_test(caller.id),
    ):
        result = asyncio.run(tool(reference=root_session_id))

    assert result["success"] is False
    assert result["error_code"] == "forbidden"
    assert terminal.state == "live"
    assert runtime.killed == []


def test_terminate_terminal_refuses_unseeded_request_context() -> None:
    terminal = make_memory_terminal(session_name="external-shell")
    terminal.ownership = "external"
    sessions = MagicMock()
    tool, runtime = _terminate_tool(terminal, sessions)

    result = asyncio.run(tool(reference=terminal.id))

    assert result["success"] is False
    assert result["error_code"] == "forbidden"
    assert terminal.state == "live"
    assert runtime.killed == []


def test_terminate_terminal_refuses_managed_agent_principal() -> None:
    terminal = make_memory_terminal(session_name="external-shell")
    terminal.ownership = "external"
    sessions = MagicMock()
    tool, runtime = _terminate_tool(terminal, sessions)

    with patch(
        "gobby.mcp_proxy.tools.sessions._terminal_termination.get_request_principal",
        AsyncMock(return_value=object()),
    ):
        result = asyncio.run(tool(reference=terminal.id))

    assert result["success"] is False
    assert result["error_code"] == "forbidden"
    assert terminal.state == "live"
    assert runtime.killed == []
