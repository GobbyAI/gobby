"""Raw terminal-input runtime and coordinator contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import pytest

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.storage.terminals import Terminal
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.native_runtime import NativeTerminalRuntime
from gobby.terminals.runtime import (
    Delivered,
    InputPayloadTooLargeError,
    TerminalWriteError,
)
from gobby.terminals.tmux_runtime import TmuxTerminalRuntime
from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator, WriteRequest
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

TmuxResult = tuple[int, str, str]


@dataclass
class _Sessions:
    effects: list[TmuxResult | BaseException] = field(default_factory=list)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def base_args(self) -> list[str]:
        return ["tmux"]

    async def _run(self, *args: str, **_kwargs: object) -> TmuxResult:
        self.calls.append(args)
        effect = self.effects.pop(0) if self.effects else (0, "", "")
        if isinstance(effect, BaseException):
            raise effect
        return effect


@dataclass
class _HostClient:
    writes: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False

    async def ensure_connected(self) -> None:
        return None

    async def write(self, **kwargs: Any) -> dict[str, bool]:
        self.writes.append(kwargs)
        return {"ok": True, "written": True}


def _tmux_runtime(sessions: _Sessions) -> TmuxTerminalRuntime:
    return TmuxTerminalRuntime(cast(TmuxSessionManager, sessions))


def _native_terminal() -> Terminal:
    terminal = make_memory_terminal(backend="native")
    terminal.locator = {"host_terminal_id": "host-terminal-1"}
    return terminal


@pytest.mark.asyncio
async def test_write_input_uses_send_keys_hex_and_host_input() -> None:
    sessions = _Sessions()
    tmux = _tmux_runtime(sessions)
    tmux_terminal = make_memory_terminal()
    data = bytes(range(256)) * 2 + b"z"

    assert isinstance(await tmux.write_input(tmux_terminal, data), Delivered)
    assert len(sessions.calls) == 2
    assert sessions.calls[0][:5] == ("send-keys", "-t", "%1", "-H", "00")
    assert sessions.calls[0][4:] == tuple(f"{byte:02x}" for byte in data[:512])
    assert sessions.calls[1][4:] == ("7a",)

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

    tmux_calls = len(sessions.calls)
    host_calls = len(host.writes)
    with pytest.raises(InputPayloadTooLargeError):
        await tmux.write_input(tmux_terminal, b"x" * (64 * 1024 + 1))
    with pytest.raises(InputPayloadTooLargeError):
        await native.write_input(native_terminal, b"x" * (64 * 1024 + 1))
    assert len(sessions.calls) == tmux_calls
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


@pytest.mark.parametrize(
    ("effects", "data_size", "stage", "delivered_bytes"),
    [
        ([(1, "", "first failed")], 513, "none", 0),
        ([(0, "", ""), (1, "", "middle failed")], 1025, "partial", 512),
        (
            [(0, "", ""), (0, "", ""), (1, "", "final failed")],
            1025,
            "partial",
            1024,
        ),
    ],
)
@pytest.mark.asyncio
async def test_chunked_write_classifies_partial_delivery(
    effects: list[TmuxResult | BaseException],
    data_size: int,
    stage: str,
    delivered_bytes: int,
) -> None:
    sessions = _Sessions(effects=list(effects))

    with pytest.raises(TerminalWriteError) as error:
        await _tmux_runtime(sessions).write_input(make_memory_terminal(), b"x" * data_size)

    assert error.value.stage == stage
    assert error.value.delivered_bytes == delivered_bytes


@pytest.mark.asyncio
async def test_write_input_timeout_is_indeterminate_and_cancellation_propagates() -> None:
    timeout_sessions = _Sessions(effects=[TimeoutError("tmux command timed out")])
    with pytest.raises(TerminalWriteError) as timeout:
        await _tmux_runtime(timeout_sessions).write_input(make_memory_terminal(), b"x")
    assert timeout.value.stage == "partial"
    assert timeout.value.delivered_bytes is None

    cancelled_sessions = _Sessions(effects=[asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await _tmux_runtime(cancelled_sessions).write_input(make_memory_terminal(), b"x")
