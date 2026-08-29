"""TerminalEffectBridge timeout, cancel, loop-misuse, and shutdown drain tests."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, cast

import pytest

from gobby.terminals.runtime import IndeterminateWrite, LoopMisuse
from gobby.terminals.sync_bridge import TerminalEffectBridge
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


def _request(terminal_id: str) -> WriteRequest:
    return WriteRequest(
        terminal_id=terminal_id,
        action_key="hook-write",
        origin="automatic",
        kind="text",
        payload="from-hook",
    )


@pytest.mark.asyncio
async def test_timeout_cancel_late_completion_and_loop_misuse() -> None:
    loop = asyncio.get_running_loop()
    terminal = make_memory_terminal()
    store = MemoryTerminalStore(terminal)
    hold = asyncio.Event()
    runtime = FakeRuntime(hold=hold)
    coordinator = WriteCoordinator(cast(UnresolvedWriteStore, store), runtime_registry(runtime))
    bridge = TerminalEffectBridge(
        loop,
        coordinator,
        timeout_seconds=0.1,
    )
    on_loop = bridge.run(_request(terminal.id))
    assert isinstance(on_loop, LoopMisuse)
    assert runtime.write_log == []

    result_box: dict[str, Any] = {}

    def worker() -> None:
        result_box["pre"] = bridge.run(_request(terminal.id))

    thread = threading.Thread(target=worker)
    thread.start()
    await runtime.started.wait()
    thread.join(timeout=2)
    assert isinstance(result_box["pre"], IndeterminateWrite)
    row = store.get(terminal.id)
    assert row is not None
    assert "hook-write" in row.unresolved_writes
    hold.set()
    await asyncio.sleep(0)
    retry = await coordinator.write(_request(terminal.id))
    from gobby.terminals.runtime import Suppressed

    assert isinstance(retry, Suppressed)


@pytest.mark.asyncio
async def test_shutdown_drain_and_timeout_dispatch_race() -> None:
    from gobby.runner_lifecycle_terminal_effects import drain_terminal_effects

    loop = asyncio.get_running_loop()
    terminal = make_memory_terminal()
    store = MemoryTerminalStore(terminal)
    hold = asyncio.Event()
    runtime = FakeRuntime(hold=hold)
    coordinator = WriteCoordinator(cast(UnresolvedWriteStore, store), runtime_registry(runtime))
    bridge = TerminalEffectBridge(
        loop,
        coordinator,
        timeout_seconds=0.1,
        shutdown_timeout_seconds=0.1,
    )

    def worker() -> None:
        bridge.run(_request(terminal.id))

    thread = threading.Thread(target=worker)
    thread.start()
    await runtime.started.wait()
    thread.join(timeout=2)
    await drain_terminal_effects(bridge, timeout_seconds=0.1)
    row = store.get(terminal.id)
    assert row is not None
    assert row.automatic_write_quarantined_at is not None
    assert row.automatic_write_quarantine_action_key == "hook-write"

    restored = WriteCoordinator(cast(UnresolvedWriteStore, store), runtime_registry(FakeRuntime()))
    from gobby.terminals.runtime import AutomaticWriteQuarantined

    refused = await restored.write(
        WriteRequest(
            terminal_id=terminal.id,
            action_key="other-auto",
            origin="automatic",
            kind="text",
            payload="blocked",
        )
    )
    assert isinstance(refused, AutomaticWriteQuarantined)
