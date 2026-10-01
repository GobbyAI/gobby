"""Daemon shutdown transfers existing native input authority without granting it."""

import asyncio
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.servers.websocket.models import WebSocketConfig
from gobby.servers.websocket.server import WebSocketServer
from gobby.storage.terminals import Terminal
from gobby.terminals.input_grants import sync_host_input_grant
from gobby.terminals.leases import HolderChange, TerminalLeaseRegistry
from tests.terminals.fakes import make_memory_terminal

pytestmark = pytest.mark.unit


class _GrantRuntime:
    """The host checks writes against this grant, independent of the daemon lease."""

    def __init__(self) -> None:
        self.current: str | None = None
        self.handoffs: list[tuple[str, str]] = []

    async def grant_input(self, terminal: Terminal, attachment_id: str) -> None:
        self.current = attachment_id

    async def revoke_input(self, terminal: Terminal, attachment_id: str | None = None) -> None:
        if attachment_id is None or attachment_id == self.current:
            self.current = None


def _registry(runtime: _GrantRuntime, record_result: bool | None = True) -> TerminalLeaseRegistry:
    registry = TerminalLeaseRegistry(daemon_epoch="handoff-test")

    async def follow_holder(change: HolderChange) -> bool | None:
        if getattr(change, "reason", "lease_change") == "daemon_shutdown":
            if record_result is None:
                raise OSError("handoff storage unavailable")
            if not record_result:
                return False
            assert change.terminal is not None
            assert change.holder is not None
            runtime.handoffs.append((change.terminal.id, change.holder.attachment_id))
            return True
        return await sync_host_input_grant(runtime, change.terminal, change.holder)

    registry.set_holder_observer(follow_holder)
    return registry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "delivery", "viewer", "reason", "preserve"),
    [
        ("native", "direct", "gclient", "daemon_shutdown", True),
        ("native", "direct", "gclient", "ws_close", False),
        ("native", "proxy", "gclient", "daemon_shutdown", False),
        ("native", "direct", "web", "daemon_shutdown", False),
        ("tmux", "direct", "gclient", "daemon_shutdown", False),
    ],
)
async def test_shutdown_finalization_preserves_only_existing_direct_native_grant(
    backend: Literal["native", "tmux"],
    delivery: str,
    viewer: Literal["gclient", "web"],
    reason: str,
    preserve: bool,
) -> None:
    runtime = _GrantRuntime()
    registry = _registry(runtime)
    terminal = make_memory_terminal(backend=backend)
    socket = object()
    attachment = await registry.attach(
        terminal.id,
        delivery,
        websocket=socket,
        terminal=terminal,
        backend=backend,
        viewer=viewer,
    )
    result = await registry.take_control(terminal.id, attachment.attachment_id)
    assert result.granted
    before = runtime.current
    if preserve:
        assert before == attachment.attachment_id

    events = await registry.finalize_websocket(socket, reason)

    assert len(events) == 1
    assert events[0].reason == reason
    assert registry.holder(terminal.id) is None
    assert registry.get(attachment.attachment_id) is None
    assert runtime.current == (before if preserve else None)
    assert runtime.handoffs == ([(terminal.id, attachment.attachment_id)] if preserve else [])


@pytest.mark.asyncio
async def test_shutdown_finalization_never_creates_a_grant_for_an_observer() -> None:
    runtime = _GrantRuntime()
    registry = _registry(runtime)
    terminal = make_memory_terminal(backend="native")
    socket = object()
    attachment = await registry.attach(
        terminal.id, "direct", websocket=socket, terminal=terminal, backend="native"
    )
    assert registry.holder(terminal.id) is None
    assert runtime.current is None

    await registry.finalize_websocket(socket, "daemon_shutdown")

    assert registry.get(attachment.attachment_id) is None
    assert runtime.current is None


@pytest.mark.asyncio
@pytest.mark.parametrize("record_result", [False, None])
async def test_failed_recording_revokes_the_existing_grant(record_result: bool | None) -> None:
    runtime = _GrantRuntime()
    registry = _registry(runtime, record_result)
    terminal = make_memory_terminal(backend="native")
    attachment = await registry.attach(terminal.id, "direct", terminal=terminal)
    result = await registry.take_control(terminal.id, attachment.attachment_id)
    assert result.host_input_granted is True

    await registry.finalize(attachment.attachment_id, "daemon_shutdown")

    assert runtime.current is None
    assert runtime.handoffs == []
    assert registry.get(attachment.attachment_id) is None


@pytest.mark.asyncio
async def test_refused_host_grant_is_never_preserved() -> None:
    class RefusingRuntime(_GrantRuntime):
        async def grant_input(self, terminal: Terminal, attachment_id: str) -> None:
            raise ConnectionError("host refused grant")

    runtime = RefusingRuntime()
    registry = _registry(runtime)
    terminal = make_memory_terminal(backend="native")
    attachment = await registry.attach(terminal.id, "direct", terminal=terminal)
    result = await registry.take_control(terminal.id, attachment.attachment_id)
    assert result.host_input_granted is False

    await registry.finalize(attachment.attachment_id, "daemon_shutdown")

    assert runtime.current is None
    assert runtime.handoffs == []


@pytest.mark.asyncio
async def test_websocket_stop_marks_client_finalization_as_daemon_shutdown() -> None:
    runtime = _GrantRuntime()
    server = WebSocketServer(
        config=WebSocketConfig(), mcp_manager=MagicMock(), auth_callback=AsyncMock()
    )
    registry = _registry(runtime)
    server.lease_registry = registry

    class ClosingSocket:
        async def close(self, *, code: int, reason: str) -> None:
            assert code == 1001
            assert reason == "Server shutting down"
            await server._cleanup_terminal_client(self)

    socket = ClosingSocket()
    terminal = make_memory_terminal(backend="native")
    attachment = await registry.attach(
        terminal.id, "direct", websocket=socket, terminal=terminal, backend="native"
    )
    await registry.take_control(terminal.id, attachment.attachment_id)
    assert runtime.current == attachment.attachment_id
    server.clients[socket] = {}

    with patch.object(server, "cleanup_voice", new_callable=AsyncMock):
        await server.stop()

    assert registry.holder(terminal.id) is None
    assert runtime.current == attachment.attachment_id
    assert runtime.handoffs == [(terminal.id, attachment.attachment_id)]


@pytest.mark.asyncio
async def test_preemption_and_release_retire_authority_before_shutdown() -> None:
    runtime = _GrantRuntime()
    registry = _registry(runtime)
    terminal = make_memory_terminal(backend="native")
    old = await registry.attach(terminal.id, "direct", terminal=terminal)
    replacement = await registry.attach(terminal.id, "direct", terminal=terminal)
    await registry.take_control(terminal.id, old.attachment_id)
    result = await registry.take_control(terminal.id, replacement.attachment_id, takeover=True)
    assert result.displaced_attachment_id == old.attachment_id
    assert runtime.current == replacement.attachment_id

    await registry.finalize(old.attachment_id, "daemon_shutdown")
    assert runtime.current == replacement.attachment_id
    assert runtime.handoffs == []
    result = await registry.release_control(replacement.attachment_id)
    assert result.reason == "released"
    assert not result.granted
    assert runtime.current is None
    await registry.finalize(replacement.attachment_id, "daemon_shutdown")
    assert runtime.handoffs == []


@pytest.mark.asyncio
async def test_cancelled_recording_revokes_before_propagating_cancellation() -> None:
    runtime = _GrantRuntime()
    registry = _registry(runtime)
    terminal = make_memory_terminal(backend="native")
    attachment = await registry.attach(terminal.id, "direct", terminal=terminal)
    await registry.take_control(terminal.id, attachment.attachment_id)
    recording = asyncio.Event()

    async def follow_holder(change: HolderChange) -> bool | None:
        if change.reason == "daemon_shutdown":
            recording.set()
            await asyncio.Event().wait()
        return await sync_host_input_grant(runtime, change.terminal, change.holder)

    registry.set_holder_observer(follow_holder)
    finalize = asyncio.create_task(registry.finalize(attachment.attachment_id, "daemon_shutdown"))
    try:
        await asyncio.wait_for(recording.wait(), 1.0)
        finalize.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(finalize, 1.0)
        assert runtime.current is None
    finally:
        finalize.cancel()
        await asyncio.gather(finalize, return_exceptions=True)
