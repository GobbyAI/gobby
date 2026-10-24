"""A direct input lease grants a host stream, never just a daemon holder name."""

from dataclasses import dataclass

import pytest

from gobby.storage.terminals import Terminal
from gobby.terminals.input_grants import sync_host_input_grant
from tests.terminals.fakes import make_memory_terminal

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@dataclass
class Holder:
    attachment_id: str = "holder"
    frame_delivery: str = "direct"
    host_attachment_id: int | None = 42


class Runtime:
    def __init__(self) -> None:
        self.grants: list[tuple[str, str, int | None]] = []
        self.revokes: list[str] = []

    async def grant_input(
        self, terminal: Terminal, attachment_id: str, host_attachment_id: int | None = None
    ) -> None:
        self.grants.append((terminal.id, attachment_id, host_attachment_id))

    async def revoke_input(self, terminal: Terminal, attachment_id: str | None = None) -> None:
        self.revokes.append(terminal.id)


async def test_direct_grant_carries_the_host_stream_identifier() -> None:
    runtime = Runtime()
    terminal = make_memory_terminal(backend="native")
    result = await sync_host_input_grant(runtime, terminal, Holder())
    assert result is True
    assert runtime.grants == [(terminal.id, "holder", 42)]
    assert runtime.revokes == []


async def test_direct_grant_without_a_host_stream_revokes_stale_authority() -> None:
    runtime = Runtime()
    terminal = make_memory_terminal(backend="native")
    result = await sync_host_input_grant(runtime, terminal, Holder(host_attachment_id=None))
    assert result is False
    assert runtime.grants == []
    assert runtime.revokes == [terminal.id]


async def test_refused_replacement_grant_revokes_the_previous_stream() -> None:
    class RefusingRuntime(Runtime):
        async def grant_input(
            self, terminal: Terminal, attachment_id: str, host_attachment_id: int | None = None
        ) -> None:
            raise OSError("replacement host attachment is gone")

    runtime = RefusingRuntime()
    terminal = make_memory_terminal(backend="native")
    result = await sync_host_input_grant(runtime, terminal, Holder())
    assert result is False
    assert runtime.revokes == [terminal.id]


async def test_take_control_forwards_the_authenticated_holders_host_stream() -> None:
    from gobby.terminals.leases import HolderChange, TerminalLeaseRegistry

    runtime = Runtime()
    terminal = make_memory_terminal(backend="native")
    registry = TerminalLeaseRegistry(daemon_epoch="host-stream-grant")

    async def follow_holder(change: HolderChange) -> bool | None:
        return await sync_host_input_grant(runtime, change.terminal, change.holder)

    registry.set_holder_observer(follow_holder)
    attachment = await registry.attach(terminal.id, "direct", terminal=terminal)
    result = await registry.take_control(
        terminal.id, attachment.attachment_id, host_attachment_id=42
    )
    assert result.granted
    assert result.host_input_granted is True
    assert runtime.grants == [(terminal.id, attachment.attachment_id, 42)]
