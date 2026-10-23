"""Remote terminal writes travel through the node that owns the terminal."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
import pytest

from gobby.hub import remote_terminal
from gobby.hub.caller_scope import bind_caller, reset_caller
from gobby.hub.node_client import NodeCommandError
from gobby.hub.remote_terminal import NodeTerminalWriteError
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.runtime import Delivered, IndeterminateWrite, TerminalWriteError
from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator, WriteRequest
from gobby.utils.machine_id import require_machine_id
from tests.terminals.fakes import (
    FakeRuntime,
    MachineOwnerRows,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

pytestmark = pytest.mark.unit

TEST_USER_ID = "20000000-0000-4000-8000-000000000001"
OTHER_USER_ID = "20000000-0000-4000-8000-000000000002"
REMOTE_MACHINE = "30000000-0000-4000-8000-000000000003"


class _FakeNode:
    """Scripted node command client that records every command it receives."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []
        self.error: NodeCommandError | None = None

    async def send(
        self,
        machine_id: str,
        command: str,
        payload: dict[str, Any],
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        self.sent.append((machine_id, command, payload))
        if self.error is not None:
            raise self.error
        return {}


class _MachineRows:
    """In-memory machines table that counts ownership lookups."""

    def __init__(self, owners: dict[str, str]) -> None:
        self.owners = owners
        self.calls = 0

    def fetchone(self, _sql: str, params: tuple[object, ...]) -> dict[str, object] | None:
        self.calls += 1
        machine_id = str(params[0])
        owner = self.owners.get(machine_id)
        if owner is None:
            return None
        now = datetime.now(UTC)
        return {
            "id": machine_id,
            "owner_user_id": owner,
            "first_seen": now,
            "last_seen": now,
            "ref": 1,
        }


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> _FakeNode:
    fake = _FakeNode()
    monkeypatch.setattr(remote_terminal, "node_command_client", lambda: fake)
    return fake


def _coordinator(machine_id: str) -> tuple[WriteCoordinator, FakeRuntime, MemoryTerminalStore, str]:
    terminal = make_memory_terminal(machine_id=machine_id)
    store = MemoryTerminalStore(terminal)
    runtime = FakeRuntime()
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(runtime),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )
    return coordinator, runtime, store, terminal.id


def _write(terminal_id: str, *, payload: str = "ls") -> WriteRequest:
    return WriteRequest(
        terminal_id=terminal_id,
        action_key="act-1",
        origin="automatic",
        kind="text",
        payload=payload,
        submit=True,
        idempotency_key="idem-1",
    )


def _unresolved(store: MemoryTerminalStore, terminal_id: str) -> dict[str, Any]:
    terminal = store.get(terminal_id)
    assert terminal is not None
    return terminal.unresolved_writes


def _latched(store: MemoryTerminalStore, terminal_id: str) -> bool:
    return "act-1" in _unresolved(store, terminal_id)


@pytest.mark.asyncio
async def test_remote_write_uses_node_command(node: _FakeNode) -> None:
    coordinator, runtime, _, terminal_id = _coordinator(REMOTE_MACHINE)

    outcome = await coordinator.write(_write(terminal_id))

    assert isinstance(outcome, Delivered)
    assert node.sent == [
        (
            REMOTE_MACHINE,
            "terminal.write",
            {"terminal_id": terminal_id, "kind": "text", "data": "ls", "submit": True},
        )
    ]
    assert runtime.write_log == []


@pytest.mark.asyncio
async def test_offline_node_clears_latch(node: _FakeNode) -> None:
    coordinator, runtime, store, terminal_id = _coordinator(REMOTE_MACHINE)
    node.error = NodeCommandError("node_offline", "Target has no live channel", "none")

    with pytest.raises(NodeTerminalWriteError) as excinfo:
        await coordinator.write(_write(terminal_id))

    assert excinfo.value.code == "node_offline"
    assert excinfo.value.stage == "none"
    assert not _latched(store, terminal_id)
    assert runtime.write_log == []


@pytest.mark.asyncio
async def test_partial_stage_propagates(node: _FakeNode) -> None:
    coordinator, _, store, terminal_id = _coordinator(REMOTE_MACHINE)
    node.error = NodeCommandError("command_failed", "Host failed after the write", "partial")

    with pytest.raises(NodeTerminalWriteError) as excinfo:
        await coordinator.write(_write(terminal_id))

    assert excinfo.value.stage == "partial"
    assert _latched(store, terminal_id)


@pytest.mark.asyncio
async def test_local_write_unchanged(node: _FakeNode) -> None:
    local = require_machine_id()
    coordinator, runtime, _, terminal_id = _coordinator(local)
    database = _MachineRows({local: TEST_USER_ID})

    token = bind_caller(TEST_USER_ID, database)
    try:
        outcome = await coordinator.write(_write(terminal_id))
    finally:
        reset_caller(token)

    assert isinstance(outcome, Delivered)
    assert runtime.write_log == [("text", "ls\n")]
    assert node.sent == []
    assert database.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["node_timeout", "node_reply_lost"])
async def test_lost_reply_is_indeterminate(node: _FakeNode, code: str) -> None:
    coordinator, _, store, terminal_id = _coordinator(REMOTE_MACHINE)
    node.error = NodeCommandError(code, "Reply lost", None)

    first = await coordinator.write(_write(terminal_id))
    second = await coordinator.write(_write(terminal_id))

    assert isinstance(first, IndeterminateWrite)
    assert isinstance(second, IndeterminateWrite)
    assert _latched(store, terminal_id)
    assert len(node.sent) == 1


@pytest.mark.asyncio
async def test_write_needs_owned_machine(node: _FakeNode) -> None:
    local = require_machine_id()
    database = _MachineRows({local: TEST_USER_ID, REMOTE_MACHINE: TEST_USER_ID})

    for machine_id in (local, REMOTE_MACHINE):
        coordinator, runtime, store, terminal_id = _coordinator(machine_id)
        token = bind_caller(OTHER_USER_ID, database)
        try:
            with pytest.raises(NodeTerminalWriteError) as excinfo:
                await coordinator.write(_write(terminal_id))
        finally:
            reset_caller(token)

        assert excinfo.value.code == "machine_not_owned"
        assert excinfo.value.stage == "none"
        assert not _latched(store, terminal_id)
        assert runtime.write_log == []
        assert node.sent == []

    coordinator, runtime, _, local_id = _coordinator(local)
    token = bind_caller(TEST_USER_ID, database)
    try:
        assert isinstance(await coordinator.write(_write(local_id)), Delivered)
    finally:
        reset_caller(token)
    assert runtime.write_log == [("text", "ls\n")]

    coordinator, _, _, remote_id = _coordinator(REMOTE_MACHINE)
    token = bind_caller(TEST_USER_ID, database)
    try:
        assert isinstance(await coordinator.write(_write(remote_id)), Delivered)
    finally:
        reset_caller(token)
    assert [machine for machine, _, _ in node.sent] == [REMOTE_MACHINE]

    coordinator, _, _, unbound_id = _coordinator(REMOTE_MACHINE)
    assert isinstance(await coordinator.write(_write(unbound_id)), Delivered)
    assert len(node.sent) == 2


@pytest.mark.asyncio
async def test_remote_refuses_client_fd_and_unnamed_keys(node: _FakeNode) -> None:
    coordinator, runtime, store, terminal_id = _coordinator(REMOTE_MACHINE)
    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(TerminalWriteError) as excinfo:
            await coordinator.write(replace(_write(terminal_id), client_fd=write_fd))

        assert excinfo.value.stage == "none"
        os.set_blocking(read_fd, False)
        with pytest.raises(BlockingIOError):
            os.read(read_fd, 1)

        with pytest.raises(TerminalWriteError) as excinfo:
            await coordinator.write(replace(_write(terminal_id), kind="key", payload="ls -la"))

        assert excinfo.value.stage == "none"
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert node.sent == []
    assert runtime.write_log == []
    assert _unresolved(store, terminal_id) == {}


@pytest.mark.asyncio
async def test_ownership_read_failure_clears_latch(node: _FakeNode) -> None:
    """A machines read that fails sends nothing and clears the latch like any refusal."""
    coordinator, runtime, store, terminal_id = _coordinator(REMOTE_MACHINE)
    database = MachineOwnerRows({}, error=psycopg.OperationalError("database unavailable"))
    token = bind_caller(TEST_USER_ID, database)
    try:
        with pytest.raises(NodeTerminalWriteError) as excinfo:
            await coordinator.write(_write(terminal_id))
    finally:
        reset_caller(token)

    assert excinfo.value.code == "machine_ownership_unavailable"
    assert excinfo.value.stage == "none"
    assert not _latched(store, terminal_id)
    assert runtime.write_log == []
    assert node.sent == []
