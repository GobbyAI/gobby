"""Indeterminate-write abstain, latch, capacity, and restart tests (plan 2.4)."""

from __future__ import annotations

from typing import Any, Literal, cast

import pytest

from gobby.storage.terminals import (
    UNRESOLVED_WRITE_MAX_ENTRIES,
    UnresolvedWriteCapacityError,
)
from gobby.terminals.runtime import Delivered, IndeterminateWrite, Suppressed
from gobby.terminals.write_coordinator import (
    SequenceDelay,
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


def _coordinator(
    runtime: FakeRuntime | None = None,
    *,
    unresolved: dict[str, Any] | None = None,
    backend: Literal["tmux", "native"] = "tmux",
) -> tuple[WriteCoordinator, FakeRuntime, MemoryTerminalStore]:
    terminal = make_memory_terminal(backend=backend, unresolved_writes=unresolved)
    store = MemoryTerminalStore(terminal)
    fake = runtime or FakeRuntime(backend=backend)
    coordinator = WriteCoordinator(cast(UnresolvedWriteStore, store), runtime_registry(fake))
    return coordinator, fake, store


def _auto(terminal_id: str, action_key: str, payload: str) -> WriteRequest:
    return WriteRequest(
        terminal_id=terminal_id,
        action_key=action_key,
        origin="automatic",
        kind="text",
        payload=payload,
    )


@pytest.mark.asyncio
async def test_unresolved_write_latch_suppresses_only_the_same_action() -> None:
    coordinator, runtime, store = _coordinator()
    terminal = next(iter(store.rows.values()))
    runtime.outcome = IndeterminateWrite(detail="lost")
    await coordinator.write(_auto(terminal.id, "idle-reprompt", "first"))
    await coordinator.write(_auto(terminal.id, "prompt-answer", "second"))
    row = store.get(terminal.id)
    assert row is not None
    assert set(row.unresolved_writes) == {"idle-reprompt", "prompt-answer"}

    runtime.outcome = Delivered()
    suppressed = await coordinator.write(_auto(terminal.id, "idle-reprompt", "retry"))
    assert isinstance(suppressed, Suppressed)
    third = await coordinator.write(_auto(terminal.id, "watchdog-interrupt", "third"))
    assert isinstance(third, Delivered)
    operator = await coordinator.write(
        WriteRequest(
            terminal_id=terminal.id,
            action_key="operator-type",
            origin="operator",
            kind="text",
            payload="human",
            attachment_id="att-1",
            expected_lease_generation=await coordinator.grant_lease(terminal.id, "att-1"),
        )
    )
    assert isinstance(operator, Delivered)
    payloads = [payload for _kind, payload in runtime.write_log]
    assert "retry" not in payloads
    assert "third" in payloads
    assert any(item.startswith("human") for item in payloads)

    coordinator.observe_resolved(terminal.id, "idle-reprompt")
    row = store.get(terminal.id)
    assert row is not None
    assert "idle-reprompt" not in row.unresolved_writes
    assert "prompt-answer" in row.unresolved_writes

    coordinator.clear_on_exit(terminal.id)
    row = store.get(terminal.id)
    assert row is not None
    assert row.unresolved_writes == {}

    coordinator, runtime, store = _coordinator(
        unresolved={"keep-me": {"at": "2026-01-01T00:00:00+00:00", "origin": "automatic"}}
    )
    terminal = next(iter(store.rows.values()))
    restarted = WriteCoordinator(cast(UnresolvedWriteStore, store), runtime_registry(runtime))
    again = await restarted.write(_auto(terminal.id, "keep-me", "again"))
    assert isinstance(again, Suppressed)
    assert runtime.write_log == []


@pytest.mark.asyncio
async def test_indeterminate_write_abstains_at_every_consumer() -> None:
    coordinator, runtime, store = _coordinator()
    terminal = next(iter(store.rows.values()))
    runtime.outcomes = [Delivered(), IndeterminateWrite(detail="lost")]
    outcome = await coordinator.run_sequence(
        terminal.id,
        action_key="wake-sequence",
        origin="automatic",
        steps=[
            WriteRequest(
                terminal_id=terminal.id,
                action_key="wake-sequence",
                origin="automatic",
                kind="key",
                payload="escape",
            ),
            WriteRequest(
                terminal_id=terminal.id,
                action_key="wake-sequence",
                origin="automatic",
                kind="text",
                payload="hello",
            ),
            WriteRequest(
                terminal_id=terminal.id,
                action_key="wake-sequence",
                origin="automatic",
                kind="key",
                payload="enter",
            ),
        ],
    )
    assert isinstance(outcome, IndeterminateWrite)
    kinds = [kind for kind, _payload in runtime.write_log]
    assert kinds == ["key", "text"]
    row = store.get(terminal.id)
    assert row is not None
    assert "wake-sequence" in row.unresolved_writes


@pytest.mark.asyncio
async def test_unresolved_write_capacity_survives_restart() -> None:
    writes = {
        f"k{index:02d}": {"at": "2026-01-01T00:00:00+00:00", "origin": "automatic"}
        for index in range(UNRESOLVED_WRITE_MAX_ENTRIES)
    }
    coordinator, runtime, store = _coordinator(unresolved=writes)
    terminal = next(iter(store.rows.values()))
    restarted = WriteCoordinator(cast(UnresolvedWriteStore, store), runtime_registry(runtime))
    with pytest.raises(UnresolvedWriteCapacityError):
        await restarted.write(_auto(terminal.id, "one-over", "nope"))
    assert runtime.write_log == []
    restarted.clear_on_exit(terminal.id)
    delivered = await restarted.write(_auto(terminal.id, "after-exit", "ok"))
    assert isinstance(delivered, Delivered)


@pytest.mark.asyncio
async def test_write_ahead_hard_kill_suppresses_retry_across_restart() -> None:
    coordinator, runtime, store = _coordinator()
    terminal = next(iter(store.rows.values()))
    store.persist_unresolved_write(terminal.id, "auto-1", "automatic")
    restarted = WriteCoordinator(cast(UnresolvedWriteStore, store), runtime_registry(runtime))
    suppressed = await restarted.write(_auto(terminal.id, "auto-1", "retry"))
    assert isinstance(suppressed, Suppressed)
    assert runtime.write_log == []

    runtime.outcome = IndeterminateWrite(detail="lost")
    await restarted.write(_auto(terminal.id, "auto-2", "maybe"))
    restored = WriteCoordinator(cast(UnresolvedWriteStore, store), runtime_registry(FakeRuntime()))
    again = await restored.write(_auto(terminal.id, "auto-2", "retry-2"))
    assert isinstance(again, Suppressed)

    runtime.outcome = Delivered()
    delivered = await coordinator.write(_auto(terminal.id, "auto-3", "done"))
    assert isinstance(delivered, Delivered)
    after_success = WriteCoordinator(
        cast(UnresolvedWriteStore, store), runtime_registry(FakeRuntime())
    )
    follow = await after_success.write(_auto(terminal.id, "auto-4", "next"))
    assert isinstance(follow, Delivered)

    seq_runtime = FakeRuntime()
    seq_runtime.outcomes = [Delivered(), IndeterminateWrite(detail="lost")]
    seq_store = MemoryTerminalStore(make_memory_terminal())
    seq_terminal = next(iter(seq_store.rows.values()))
    seq = WriteCoordinator(cast(UnresolvedWriteStore, seq_store), runtime_registry(seq_runtime))
    outcome = await seq.run_sequence(
        seq_terminal.id,
        action_key="wake-seq",
        origin="automatic",
        steps=[
            WriteRequest(
                terminal_id=seq_terminal.id,
                action_key="wake-seq",
                origin="automatic",
                kind="key",
                payload="escape",
            ),
            SequenceDelay(seconds=0),
            WriteRequest(
                terminal_id=seq_terminal.id,
                action_key="wake-seq",
                origin="automatic",
                kind="key",
                payload="enter",
            ),
        ],
    )
    assert isinstance(outcome, IndeterminateWrite)
    restored_seq = WriteCoordinator(
        cast(UnresolvedWriteStore, seq_store), runtime_registry(FakeRuntime())
    )
    blocked = await restored_seq.run_sequence(
        seq_terminal.id,
        action_key="wake-seq",
        origin="automatic",
        steps=[
            WriteRequest(
                terminal_id=seq_terminal.id,
                action_key="wake-seq",
                origin="automatic",
                kind="key",
                payload="enter",
            )
        ],
    )
    assert isinstance(blocked, Suppressed)
