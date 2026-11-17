"""Raw terminal-input runtime and coordinator contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, cast

import pytest

from gobby.storage.terminals import Terminal
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.native_runtime import NativeTerminalRuntime
from gobby.terminals.runtime import (
    Delivered,
    InputPayloadTooLargeError,
)
from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator, WriteRequest
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)


@dataclass
class _HostClient:
    writes: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False

    async def ensure_connected(self) -> None:
        return None

    async def write(self, **kwargs: Any) -> dict[str, bool]:
        self.writes.append(kwargs)
        return {"ok": True, "written": True}


def _native_terminal() -> Terminal:
    terminal = make_memory_terminal(backend="native")
    terminal.locator = {"host_terminal_id": "host-terminal-1"}
    return terminal


@pytest.mark.asyncio
async def test_write_input_uses_host_input() -> None:
    host = _HostClient()
    native = NativeTerminalRuntime(host, frame_host_epoch="epoch-1")
    native_terminal = _native_terminal()
    assert isinstance(await native.write_input(native_terminal, b"\x04\x03\x1b[A"), Delivered)
    assert host.writes == [
        {
            "host_terminal_id": "host-terminal-1",
            "kind": "input",
            "data": b"\x04\x03\x1b[A",
            "submit": False,
            "operation_seq": None,
        }
    ]
    host_calls = len(host.writes)
    with pytest.raises(InputPayloadTooLargeError):
        await native.write_input(native_terminal, b"x" * (64 * 1024 + 1))
    assert len(host.writes) == host_calls


@pytest.mark.asyncio
async def test_coordinator_routes_by_kind() -> None:
    terminal = make_memory_terminal()
    store = MemoryTerminalStore(terminal)
    runtime = FakeRuntime()
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(runtime),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )

    cases: tuple[tuple[Literal["input", "paste", "text"], str, bool], ...] = (
        ("input", "\x04", False),
        ("paste", "literal paste", False),
        ("text", "answer", True),
    )
    for kind, payload, submit in cases:
        await coordinator.write(
            WriteRequest(
                terminal_id=terminal.id,
                action_key=f"test:{kind}",
                origin="automatic",
                kind=kind,
                payload=payload,
                submit=submit,
            )
        )

    assert runtime.write_log == [
        ("input", b"\x04"),
        ("paste", "literal paste"),
        ("text", "answer\n"),
    ]
