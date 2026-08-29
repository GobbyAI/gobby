"""MCP send_keys and capture_output are backend-neutral (plan 2.4.6)."""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._terminal import register_terminal_tools
from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_send_and_capture_are_backend_neutral() -> None:
    terminal = make_memory_terminal(backend="native")
    store = MemoryTerminalStore(terminal)
    runtime = FakeRuntime(backend="native")
    runtime.snapshot_text = "pane"
    registry = TerminalRuntimeRegistry()
    registry.register(runtime)
    coordinator = WriteCoordinator(cast(UnresolvedWriteStore, store), runtime_registry(runtime))
    tools = InternalToolRegistry(name="gobby-sessions", description="sessions")
    session_manager = MagicMock()
    db = MagicMock()
    register_terminal_tools(
        tools,
        session_manager,
        db,
        terminal_manager=store,
        terminal_runtime_registry=registry,
        write_coordinator=coordinator,
    )
    send = tools.get_tool("send_keys")
    capture = tools.get_tool("capture_output")
    assert send is not None
    assert capture is not None
    assert runtime.backend == "native"
