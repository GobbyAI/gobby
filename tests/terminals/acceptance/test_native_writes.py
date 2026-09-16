"""Coordinator write acceptance against a real gterm host (plan 7.2).

| Test | Plan item it proves |
| --- | --- |
| `test_lease_takeover_settles_the_displaced_write` | 5.1.2 a write whose lease was taken over is refused and persists nothing, and the new holder's write still reaches the PTY |
| `test_send_keys_is_delivered_under_another_holder_s_lease` | 5.2.1 `send_keys` with `origin="daemon"` is delivered instead of refused stale while an operator holds the lease |
| `test_send_keys_replay_returns_the_recorded_outcome` | 5.2.4 an explicit-key retry of an unresolved write dispatches nothing and returns the stored indeterminate outcome |
| `test_send_keys_conflicting_payload_is_refused` | 5.2.2 a same-key replay with a different payload fingerprint is refused `idempotency_conflict` |
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.runtime import Delivered, IndeterminateWrite
from gobby.terminals.write_coordinator import (
    IdempotencyConflictError,
    StaleTerminalLeaseError,
    WriteCoordinator,
    WriteRequest,
)
from tests.terminals.acceptance.conftest import (
    AcceptanceHost,
    LiveTerminal,
    marker_command,
    spawn_native,
    wait_for_text,
)
from tests.terminals.fakes import MemoryTerminalStore, runtime_registry

#: The action key a `send_keys` latch is stored under.
SEND_KEYS_ACTION_KEY = "send_keys:acceptance"


@dataclass
class WriteHarness:
    """A coordinator, its lease registry, and the live terminal they drive."""

    coordinator: WriteCoordinator
    leases: TerminalLeaseRegistry
    store: MemoryTerminalStore
    live: LiveTerminal
    host: AcceptanceHost

    @property
    def terminal_id(self) -> str:
        return self.live.terminal.id


@pytest.fixture
async def write_harness(native_host: AcceptanceHost) -> AsyncIterator[WriteHarness]:
    """Wire the real coordinator stack onto one live native terminal."""
    live = await spawn_native(native_host)
    store = MemoryTerminalStore(live.terminal)
    leases = TerminalLeaseRegistry(daemon_epoch="acceptance-daemon-epoch")
    coordinator = WriteCoordinator(
        store, runtime_registry(native_host.runtime), lease_registry=leases
    )
    yield WriteHarness(
        coordinator=coordinator, leases=leases, store=store, live=live, host=native_host
    )


async def test_lease_takeover_settles_the_displaced_write(write_harness: WriteHarness) -> None:
    """The displaced holder is refused and nothing is latched; the new one writes."""
    terminal_id = write_harness.terminal_id
    leases = write_harness.leases
    first = await leases.attach(terminal_id, attachment_id="acceptance-attachment-one")
    second = await leases.attach(terminal_id, attachment_id="acceptance-attachment-two")
    held = await leases.take_control(terminal_id, first.attachment_id)
    assert held.granted is True

    command, marker = marker_command("HELD")
    delivered = await write_harness.coordinator.write(
        _operator_write(terminal_id, command, first.attachment_id, held.lease_generation)
    )
    assert isinstance(delivered, Delivered)
    await wait_for_text(write_harness.live, marker, description="the lease holder's write")
    assert write_harness.store.rows[terminal_id].unresolved_writes == {}

    taken = await leases.take_control(terminal_id, second.attachment_id, takeover=True)
    assert taken.granted is True
    assert taken.displaced_attachment_id == first.attachment_id
    assert taken.lease_generation > held.lease_generation

    stale_command, stale_marker = marker_command("DISPLACED")
    with pytest.raises(StaleTerminalLeaseError):
        await write_harness.coordinator.write(
            _operator_write(terminal_id, stale_command, first.attachment_id, held.lease_generation)
        )
    assert write_harness.store.rows[terminal_id].unresolved_writes == {}

    successor_command, successor_marker = marker_command("SUCCESSOR")
    successor = await write_harness.coordinator.write(
        _operator_write(
            terminal_id, successor_command, second.attachment_id, taken.lease_generation
        )
    )
    assert isinstance(successor, Delivered)
    screen = await wait_for_text(
        write_harness.live, successor_marker, description="the new holder's write"
    )
    assert stale_marker not in screen


async def test_send_keys_is_delivered_under_another_holder_s_lease(
    write_harness: WriteHarness,
) -> None:
    """A daemon-origin `send_keys` is delivered while an operator holds control."""
    terminal_id = write_harness.terminal_id
    holder = await write_harness.leases.attach(
        terminal_id, attachment_id="acceptance-operator-holder"
    )
    granted = await write_harness.leases.take_control(terminal_id, holder.attachment_id)
    assert granted.granted is True

    command, marker = marker_command("SEND-KEYS")
    outcome = await write_harness.coordinator.write(
        WriteRequest(
            terminal_id=terminal_id,
            action_key=SEND_KEYS_ACTION_KEY,
            origin="daemon",
            kind="text",
            payload=command,
            submit=True,
            idempotency_key="acceptance-send-keys-delivered",
        )
    )

    assert isinstance(outcome, Delivered)
    await wait_for_text(write_harness.live, marker, description="the daemon-origin send_keys")
    assert write_harness.store.rows[terminal_id].unresolved_writes == {}


async def test_send_keys_replay_returns_the_recorded_outcome(
    write_harness: WriteHarness,
) -> None:
    """Retrying the key of an unresolved write returns the indeterminate outcome."""
    command = await _latch_indeterminate_send_keys(write_harness)

    replay = await write_harness.coordinator.write(
        _send_keys_write(write_harness.terminal_id, command)
    )

    assert isinstance(replay, IndeterminateWrite)
    assert replay.detail == "send_keys write outcome remains indeterminate"
    latched = write_harness.store.rows[write_harness.terminal_id].unresolved_writes
    assert set(latched) == {SEND_KEYS_ACTION_KEY}


async def test_send_keys_conflicting_payload_is_refused(write_harness: WriteHarness) -> None:
    """The same key with a different payload is an idempotency conflict."""
    await _latch_indeterminate_send_keys(write_harness)
    other_command, _marker = marker_command("CONFLICT")

    with pytest.raises(IdempotencyConflictError):
        await write_harness.coordinator.write(
            _send_keys_write(write_harness.terminal_id, other_command)
        )

    latched = write_harness.store.rows[write_harness.terminal_id].unresolved_writes
    assert set(latched) == {SEND_KEYS_ACTION_KEY}


def _operator_write(
    terminal_id: str, payload: str, attachment_id: str, generation: int
) -> WriteRequest:
    """Build the operator write one attachment issues under its own lease."""
    return WriteRequest(
        terminal_id=terminal_id,
        action_key=f"ws:{attachment_id}:{generation}",
        origin="operator",
        kind="text",
        payload=payload,
        submit=True,
        attachment_id=attachment_id,
        expected_lease_generation=generation,
    )


def _send_keys_write(terminal_id: str, payload: str) -> WriteRequest:
    """Build the `send_keys` write the acceptance suite retries under one key."""
    return WriteRequest(
        terminal_id=terminal_id,
        action_key=SEND_KEYS_ACTION_KEY,
        origin="daemon",
        kind="text",
        payload=payload,
        submit=False,
        idempotency_key="acceptance-send-keys-retry",
    )


async def _latch_indeterminate_send_keys(harness: WriteHarness) -> str:
    """Leave one `send_keys` write genuinely unresolved and return its payload.

    The host is stopped mid-request, so the coordinator has latched the write
    and dispatched it but can never learn whether the bytes landed. That is the
    state an explicit idempotency key exists for.
    """
    host_pid = harness.host.manager.host_pid
    assert host_pid is not None
    command, _marker = marker_command("INDETERMINATE")
    os.kill(host_pid, signal.SIGSTOP)
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                harness.coordinator.write(_send_keys_write(harness.terminal_id, command)),
                timeout=1.0,
            )
    finally:
        os.kill(host_pid, signal.SIGCONT)
    latched = harness.store.rows[harness.terminal_id].unresolved_writes
    assert set(latched) == {SEND_KEYS_ACTION_KEY}
    assert latched[SEND_KEYS_ACTION_KEY]["payload_fingerprint"]
    return command
