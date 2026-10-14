"""Operator terminal requests must not wait on the shared default executor (#23080).

Each test occupies the loop's only default-executor worker for the whole call,
the way unrelated ``asyncio.to_thread`` work holds it under fleet load, and
proves the operator path still answers within a bound.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.servers.websocket.terminal_sizing import TerminalSizingMixin
from gobby.storage.terminals import Terminal
from gobby.terminals.input_grants import sync_host_input_grant
from gobby.terminals.leases import SizingDecision, TerminalLeaseRegistry
from gobby.terminals.operator_lane import OPERATOR_LANE_THREAD_PREFIX
from gobby.terminals.workspace_ops import WorkspaceOps
from gobby.terminals.write_coordinator import (
    UnresolvedWriteStore,
    WriteCoordinator,
    WriteRequest,
)
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

pytestmark = pytest.mark.unit


# Far below the 2 s take-control and 5 s request deadlines gclient enforces.
OPERATOR_BOUND_SECONDS = 1.0


@asynccontextmanager
async def _default_executor_held() -> AsyncIterator[None]:
    """Hold the loop's only default-executor worker until the block exits."""
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    def occupy() -> None:
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as default_executor:
        loop.set_default_executor(default_executor)
        blocker = loop.run_in_executor(None, occupy)
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            yield
        finally:
            release.set()
            await blocker


def _run_while_default_executor_held[T](operation: Awaitable[T]) -> T:
    async def exercise() -> T:
        async with _default_executor_held():
            return await asyncio.wait_for(operation, timeout=OPERATOR_BOUND_SECONDS)

    return asyncio.run(exercise())


def _recording(threads: list[str], value: object = None) -> Any:
    def record(*_args: object, **_kwargs: object) -> object:
        threads.append(threading.current_thread().name)
        return value

    return record


def test_default_workspace_attach_answers_while_the_default_executor_is_held() -> None:
    """gclient's attach (no workspace, no project) resolves, sweeps and reads on the lane."""
    threads: list[str] = []
    machine = SimpleNamespace(id="machine-1", ref=None, hostname="local")
    workspace = SimpleNamespace(id="ws-1")
    target = SimpleNamespace(tab=None, pane=None, workspace=workspace, node=machine)
    empty_change = SimpleNamespace(removed_panes=(), removed_tabs=())
    workspaces = MagicMock()
    workspaces.resolve_node.side_effect = _recording(threads, machine)
    workspaces.create.side_effect = _recording(threads, (workspace, False))
    workspaces.sweep_dead_panes.side_effect = _recording(threads, empty_change)
    workspaces.resolve_reference.side_effect = _recording(threads, target)
    workspaces.get.side_effect = _recording(threads, workspace)
    workspaces.list_tabs.side_effect = _recording(threads, ())
    workspaces.list_panes.side_effect = _recording(threads, ())
    ops = WorkspaceOps(
        workspaces=workspaces,
        terminals=MagicMock(),
        registry=MagicMock(),
        coordinator=MagicMock(),
        sessions=MagicMock(),
        publish=MagicMock(),
    )

    with patch("gobby.terminals.workspace_contract.require_machine_id", return_value="machine-1"):
        snapshot = _run_while_default_executor_held(ops.workspace_snapshot("operator"))

    assert snapshot.workspace.id == "ws-1"
    assert snapshot.tabs == ()
    assert snapshot.panes == ()
    assert len(threads) == 8
    assert all(name.startswith(OPERATOR_LANE_THREAD_PREFIX) for name in threads)


def test_operator_keystroke_answers_while_the_default_executor_is_held() -> None:
    """An operator keystroke takes no worker-thread hop on its way to the backend."""
    terminal = make_memory_terminal()
    runtime = FakeRuntime()
    leases = TerminalLeaseRegistry(daemon_epoch="test-epoch")
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, MemoryTerminalStore(terminal)),
        runtime_registry(runtime),
        lease_registry=leases,
    )

    async def type_one_key() -> object:
        await leases.attach(terminal.id, attachment_id="att-1")
        taken = await leases.take_control(terminal.id, "att-1")
        assert taken.granted
        return await coordinator.write(
            WriteRequest(
                terminal_id=terminal.id,
                action_key="ws:att-1:1",
                origin="operator",
                kind="input",
                payload="x",
                attachment_id="att-1",
                expected_lease_generation=taken.lease_generation,
                terminal=terminal,
            )
        )

    outcome = _run_while_default_executor_held(type_one_key())

    assert type(outcome).__name__ == "Delivered"
    assert runtime.write_log == [("input", b"x")]


class _Sizer(TerminalSizingMixin):
    def __init__(self, terminal: Terminal, runtime: Any) -> None:
        self.threads: list[str] = []
        self.terminal_manager = MagicMock()
        self.terminal_manager.get.side_effect = _recording(self.threads, terminal)
        self.terminal_manager.set_dims.side_effect = _recording(self.threads)
        self._runtime = runtime

    def _runtime_for(self, backend: str) -> Any | None:
        del backend
        return self._runtime


def test_take_control_sizing_answers_while_the_default_executor_is_held() -> None:
    """The row read and dims write a granted take applies run on the lane."""
    terminal = make_memory_terminal(backend="native")
    runtime = MagicMock()

    async def resize(_row: Terminal, _rows: int, _cols: int) -> None:
        return None

    runtime.resize.side_effect = resize
    sizer = _Sizer(terminal, runtime)
    # make_memory_terminal stores 24x80; a different size forces the resize.
    decision = SizingDecision(owner_viewer="gclient", rows=25, cols=80)

    _run_while_default_executor_held(sizer._apply_terminal_sizing(terminal.id, decision))

    runtime.resize.assert_called_once()
    assert len(sizer.threads) == 2
    assert all(name.startswith(OPERATOR_LANE_THREAD_PREFIX) for name in sizer.threads)


def test_take_control_input_grant_answers_while_the_default_executor_is_held() -> None:
    """Granting host input to a direct holder clears its handoff on the lane."""
    terminal = make_memory_terminal(backend="native")
    threads: list[str] = []
    manager = MagicMock()
    manager.clear_native_input_handoff.side_effect = _recording(threads)
    runtime = MagicMock()

    async def grant_input(_terminal: Terminal, _attachment_id: str) -> None:
        return None

    runtime.grant_input.side_effect = grant_input
    holder = SimpleNamespace(attachment_id="att-1", frame_delivery="direct")

    granted = _run_while_default_executor_held(
        sync_host_input_grant(runtime, terminal, holder, manager=manager)
    )

    assert granted is True
    assert len(threads) == 1
    assert threads[0].startswith(OPERATOR_LANE_THREAD_PREFIX)
