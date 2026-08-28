"""WriteCoordinator latch, lock, lease, and sequence tests (plan 2.2)."""

from __future__ import annotations

import asyncio
from typing import Any, Literal, cast

import pytest

from gobby.storage.terminals import (
    UNRESOLVED_WRITE_ACTION_KEY_MAX_BYTES,
    UNRESOLVED_WRITE_MAX_ENTRIES,
    UnresolvedWriteCapacityError,
)
from gobby.terminals.runtime import Delivered, IndeterminateWrite
from gobby.terminals.write_coordinator import (
    SequenceDelay,
    StaleTerminalLeaseError,
    UnresolvedWriteStore,
    WriteCoordinator,
    WriteRequest,
)
from tests.terminals.fakes import FakeRuntime, MemoryTerminalStore, make_memory_terminal


def _unresolved(store: MemoryTerminalStore, terminal_id: str) -> dict[str, Any]:
    row = store.get(terminal_id)
    assert row is not None
    return row.unresolved_writes


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
    coordinator = WriteCoordinator(cast(UnresolvedWriteStore, store), fake)
    return coordinator, fake, store


@pytest.mark.asyncio
async def test_attention_and_lease_writes_serialize() -> None:
    hold = asyncio.Event()
    runtime = FakeRuntime(hold=hold)
    recapture_at: list[int] = []
    coordinator, _fake, store = _coordinator(runtime)
    terminal = store.get(next(iter(store.rows)))
    assert terminal is not None

    async def recapture(_terminal: Any) -> None:
        recapture_at.append(1)
        assert coordinator.lock_held(terminal.id)

    coordinator.set_attention_gate(recapture)
    await coordinator.grant_lease(terminal.id, "att-1")

    async def attention() -> None:
        await coordinator.write(
            WriteRequest(
                terminal_id=terminal.id,
                action_key="attn-1",
                origin="attention",
                kind="text",
                payload="attention",
            )
        )

    async def lease_holder() -> None:
        await runtime.started.wait()
        await coordinator.write(
            WriteRequest(
                terminal_id=terminal.id,
                action_key="lease-1",
                origin="operator",
                kind="text",
                payload="lease",
                attachment_id="att-1",
                expected_lease_generation=1,
            )
        )

    task_a = asyncio.create_task(attention())
    task_b = asyncio.create_task(lease_holder())
    await runtime.started.wait()
    assert recapture_at == [1]
    hold.set()
    await asyncio.gather(task_a, task_b)
    payloads = [payload for _kind, payload in runtime.write_log]
    assert payloads[0] == "attention"
    assert payloads[1].startswith("lease")


@pytest.mark.asyncio
async def test_coordinator_owns_lock_identity_and_latch() -> None:
    coordinator, runtime, store = _coordinator()
    terminal = next(iter(store.rows.values()))
    await coordinator.write(
        WriteRequest(
            terminal_id=terminal.id,
            action_key="a1",
            origin="automatic",
            kind="text",
            payload="same",
        )
    )
    runtime.outcome = IndeterminateWrite(detail="lost")
    await coordinator.write(
        WriteRequest(
            terminal_id=terminal.id,
            action_key="a2",
            origin="automatic",
            kind="text",
            payload="same",
        )
    )
    assert "a1" not in _unresolved(store, terminal.id)
    assert "a2" in _unresolved(store, terminal.id)

    await runtime.write_text(terminal, "bypass", submit=False)
    assert "bypass" not in _unresolved(store, terminal.id)

    recapture_under_lock = []

    async def recapture(_terminal: Any) -> None:
        recapture_under_lock.append(coordinator.lock_held(terminal.id))

    coordinator.set_attention_gate(recapture)
    await coordinator.write(
        WriteRequest(
            terminal_id=terminal.id,
            action_key="attn-cas",
            origin="attention",
            kind="text",
            payload="cas",
        )
    )
    assert recapture_under_lock == [True]


@pytest.mark.asyncio
async def test_sequence_holds_lock_across_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, runtime, store = _coordinator()
    terminal = next(iter(store.rows.values()))
    await coordinator.grant_lease(terminal.id, "att-1")
    delay_started = asyncio.Event()
    original_sleep = asyncio.sleep

    async def marked_sleep(delay: float) -> None:
        delay_started.set()
        await original_sleep(delay)

    runtime.outcome = Delivered()
    interleaved: list[str] = []

    async def interloper() -> None:
        await delay_started.wait()
        interleaved.append("trying")
        await coordinator.write(
            WriteRequest(
                terminal_id=terminal.id,
                action_key="lease-text",
                origin="operator",
                kind="text",
                payload="interleave",
                attachment_id="att-1",
                expected_lease_generation=1,
            )
        )
        interleaved.append("done")

    monkey_seq: list[WriteRequest | SequenceDelay] = [
        WriteRequest(
            terminal_id=terminal.id,
            action_key="wake",
            origin="automatic",
            kind="key",
            payload="escape",
        ),
        SequenceDelay(0.05),
        WriteRequest(
            terminal_id=terminal.id,
            action_key="wake",
            origin="automatic",
            kind="text",
            payload="hello",
        ),
        SequenceDelay(0.05),
        WriteRequest(
            terminal_id=terminal.id,
            action_key="wake",
            origin="automatic",
            kind="key",
            payload="enter",
        ),
    ]
    task = asyncio.create_task(interloper())
    monkeypatch.setattr("gobby.terminals.write_coordinator.asyncio.sleep", marked_sleep)
    await coordinator.run_sequence(
        terminal.id,
        action_key="wake",
        origin="automatic",
        steps=monkey_seq,
    )
    await task
    kinds = [kind for kind, _payload in runtime.write_log]
    assert kinds[0] == "key"
    assert "text" in kinds
    assert kinds[-2] == "key" or kinds[-1] == "key"
    assert interleaved == ["trying", "done"]
    assert runtime.write_log[-1][1] in {"interleave", "interleave\n", "enter"}

    runtime.write_log.clear()
    runtime.outcome = IndeterminateWrite(detail="middle")
    hold = asyncio.Event()
    hold.set()
    runtime.hold = None
    await coordinator.run_sequence(
        terminal.id,
        action_key="wake-indeterminate",
        origin="automatic",
        steps=[
            WriteRequest(
                terminal_id=terminal.id,
                action_key="wake-indeterminate",
                origin="automatic",
                kind="key",
                payload="escape",
            ),
            WriteRequest(
                terminal_id=terminal.id,
                action_key="wake-indeterminate",
                origin="automatic",
                kind="text",
                payload="body",
            ),
            WriteRequest(
                terminal_id=terminal.id,
                action_key="wake-indeterminate",
                origin="automatic",
                kind="key",
                payload="enter",
            ),
        ],
    )
    payloads = [payload for _kind, payload in runtime.write_log]
    assert "enter" not in payloads
    assert "wake-indeterminate" in _unresolved(store, terminal.id)


@pytest.mark.asyncio
async def test_sequence_cancellation_settles_once() -> None:
    for backend in ("tmux", "native"):
        hold = asyncio.Event()
        runtime = FakeRuntime(backend=backend, hold=hold)
        coordinator, _runtime, store = _coordinator(runtime, backend=backend)
        terminal = next(iter(store.rows.values()))
        task = asyncio.create_task(
            coordinator.run_sequence(
                terminal.id,
                action_key="seq-pre",
                origin="automatic",
                steps=[
                    SequenceDelay(30),
                    WriteRequest(
                        terminal_id=terminal.id,
                        action_key="seq-pre",
                        origin="automatic",
                        kind="text",
                        payload="one",
                    ),
                ],
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _unresolved(store, terminal.id) == {}
        assert runtime.write_log == []

        hold2 = asyncio.Event()
        runtime2 = FakeRuntime(backend=backend, hold=hold2)
        coordinator2, _r, store2 = _coordinator(runtime2, backend=backend)
        terminal2 = next(iter(store2.rows.values()))
        task2 = asyncio.create_task(
            coordinator2.run_sequence(
                terminal2.id,
                action_key="seq-mid",
                origin="automatic",
                steps=[
                    WriteRequest(
                        terminal_id=terminal2.id,
                        action_key="seq-mid",
                        origin="automatic",
                        kind="key",
                        payload="escape",
                    ),
                    WriteRequest(
                        terminal_id=terminal2.id,
                        action_key="seq-mid",
                        origin="automatic",
                        kind="key",
                        payload="enter",
                    ),
                ],
            )
        )
        await runtime2.started.wait()
        task2.cancel()
        hold2.set()
        with pytest.raises(asyncio.CancelledError):
            await task2
        await asyncio.sleep(0)
        assert "seq-mid" in _unresolved(store2, terminal2.id)
        payloads = [payload for _kind, payload in runtime2.write_log]
        assert "enter" not in payloads


@pytest.mark.asyncio
async def test_lease_revalidated_immediately_before_effect() -> None:
    hold = asyncio.Event()
    started = asyncio.Event()
    runtime = FakeRuntime(hold=hold)
    runtime.started = started
    coordinator, _fake, store = _coordinator(runtime)
    terminal = next(iter(store.rows.values()))
    await coordinator.grant_lease(terminal.id, "att-1")

    async def first() -> None:
        await coordinator.write(
            WriteRequest(
                terminal_id=terminal.id,
                action_key="hold",
                origin="automatic",
                kind="text",
                payload="first",
            )
        )

    holder = asyncio.create_task(first())
    await started.wait()
    takeover_task = asyncio.create_task(coordinator.takeover_lease(terminal.id, "att-2"))
    await asyncio.sleep(0)

    async def waiting_operator() -> None:
        with pytest.raises(StaleTerminalLeaseError):
            await coordinator.write(
                WriteRequest(
                    terminal_id=terminal.id,
                    action_key="op",
                    origin="operator",
                    kind="text",
                    payload="should-not-land",
                    attachment_id="att-1",
                    expected_lease_generation=1,
                )
            )

    waiter = asyncio.create_task(waiting_operator())
    await asyncio.sleep(0)
    hold.set()
    await holder
    await takeover_task
    await waiter
    assert all(payload != "should-not-land" for _kind, payload in runtime.write_log)

    await coordinator.write(
        WriteRequest(
            terminal_id=terminal.id,
            action_key="attn",
            origin="attention",
            kind="text",
            payload="attention-ok",
        )
    )
    assert any(payload.startswith("attention-ok") for _kind, payload in runtime.write_log)


@pytest.mark.asyncio
async def test_unresolved_write_capacity_is_reserved_before_dispatch() -> None:
    coordinator, runtime, store = _coordinator()
    terminal = next(iter(store.rows.values()))
    too_long = "k" * (UNRESOLVED_WRITE_ACTION_KEY_MAX_BYTES + 1)
    with pytest.raises(UnresolvedWriteCapacityError):
        await coordinator.write(
            WriteRequest(
                terminal_id=terminal.id,
                action_key=too_long,
                origin="automatic",
                kind="text",
                payload="x",
            )
        )
    assert runtime.write_log == []

    filled = {
        f"k{i:02d}": {"at": "t", "origin": "automatic"} for i in range(UNRESOLVED_WRITE_MAX_ENTRIES)
    }
    coordinator32, runtime32, store32 = _coordinator(unresolved=filled)
    terminal32 = next(iter(store32.rows.values()))
    with pytest.raises(UnresolvedWriteCapacityError):
        await coordinator32.write(
            WriteRequest(
                terminal_id=terminal32.id,
                action_key="overflow-key",
                origin="automatic",
                kind="text",
                payload="x",
            )
        )
    assert runtime32.write_log == []
    assert "overflow-key" not in _unresolved(store32, terminal32.id)

    huge_origin = "o" * 70000
    coordinator_big, runtime_big, store_big = _coordinator()
    terminal_big = next(iter(store_big.rows.values()))
    store_big.rows[terminal_big.id].unresolved_writes = {}
    with pytest.raises(UnresolvedWriteCapacityError):
        store_big.persist_unresolved_write(terminal_big.id, "big", huge_origin)
    assert runtime_big.write_log == []

    existing = filled.copy()
    existing_key = next(iter(existing))
    coordinator_existing, runtime_existing, store_existing = _coordinator(unresolved=existing)
    terminal_existing = next(iter(store_existing.rows.values()))
    await coordinator_existing.grant_lease(terminal_existing.id, "att-1")
    await coordinator_existing.write(
        WriteRequest(
            terminal_id=terminal_existing.id,
            action_key=existing_key,
            origin="operator",
            kind="text",
            payload="resolve",
            attachment_id="att-1",
            expected_lease_generation=1,
        )
    )
    assert runtime_existing.write_log


@pytest.mark.asyncio
async def test_write_ahead_latch_survives_hard_kill() -> None:
    class KillAfterPersist(MemoryTerminalStore):
        def persist_unresolved_write(
            self,
            terminal_id: str,
            action_key: str,
            origin: str,
            *,
            at: Any = None,
        ) -> Any:
            super().persist_unresolved_write(terminal_id, action_key, origin, at=at)
            raise RuntimeError("hard-kill")

    terminal = make_memory_terminal()
    store = KillAfterPersist(terminal)
    runtime = FakeRuntime()
    coordinator = WriteCoordinator(cast(UnresolvedWriteStore, store), runtime)
    with pytest.raises(RuntimeError, match="hard-kill"):
        await coordinator.write(
            WriteRequest(
                terminal_id=terminal.id,
                action_key="k1",
                origin="automatic",
                kind="text",
                payload="x",
            )
        )
    assert "k1" in _unresolved(store, terminal.id)
    assert runtime.write_log == []

    class KillAfterBytes(FakeRuntime):
        async def write_text(self, terminal: Any, text: str, submit: bool) -> Any:
            self.write_log.append(("text", text))
            raise RuntimeError("killed-after-bytes")

    store2 = MemoryTerminalStore(make_memory_terminal())
    runtime2 = KillAfterBytes()
    coordinator2 = WriteCoordinator(cast(UnresolvedWriteStore, store2), runtime2)
    terminal2 = next(iter(store2.rows.values()))
    with pytest.raises(RuntimeError, match="killed-after-bytes"):
        await coordinator2.write(
            WriteRequest(
                terminal_id=terminal2.id,
                action_key="k2",
                origin="automatic",
                kind="text",
                payload="x",
            )
        )
    assert "k2" in _unresolved(store2, terminal2.id)

    store3 = MemoryTerminalStore(make_memory_terminal())
    runtime3 = FakeRuntime(outcome=Delivered())
    coordinator3 = WriteCoordinator(cast(UnresolvedWriteStore, store3), runtime3)
    terminal3 = next(iter(store3.rows.values()))
    await coordinator3.write(
        WriteRequest(
            terminal_id=terminal3.id,
            action_key="k3",
            origin="automatic",
            kind="text",
            payload="x",
        )
    )
    assert "k3" not in _unresolved(store3, terminal3.id)

    class FailTyped(FakeRuntime):
        async def write_text(self, terminal: Any, text: str, submit: bool) -> Any:
            raise RuntimeError("no-effect")

    store4 = MemoryTerminalStore(make_memory_terminal())
    runtime4 = FailTyped()
    coordinator4 = WriteCoordinator(cast(UnresolvedWriteStore, store4), runtime4)
    terminal4 = next(iter(store4.rows.values()))
    with pytest.raises(RuntimeError, match="no-effect"):
        await coordinator4.write(
            WriteRequest(
                terminal_id=terminal4.id,
                action_key="k4",
                origin="automatic",
                kind="text",
                payload="x",
            )
        )
    # Typed no-effect failure still latches until the coordinator classifies it;
    # Delivered is the path that clears. Sequence write-ahead is asserted below.

    hold = asyncio.Event()
    runtime5 = FakeRuntime(hold=hold)
    store5 = MemoryTerminalStore(make_memory_terminal())
    coordinator5 = WriteCoordinator(cast(UnresolvedWriteStore, store5), runtime5)
    terminal5 = next(iter(store5.rows.values()))
    task = asyncio.create_task(
        coordinator5.run_sequence(
            terminal5.id,
            action_key="seq-kill",
            origin="automatic",
            steps=[
                WriteRequest(
                    terminal_id=terminal5.id,
                    action_key="seq-kill",
                    origin="automatic",
                    kind="key",
                    payload="escape",
                ),
                SequenceDelay(10),
                WriteRequest(
                    terminal_id=terminal5.id,
                    action_key="seq-kill",
                    origin="automatic",
                    kind="key",
                    payload="enter",
                ),
            ],
        )
    )
    await runtime5.started.wait()
    task.cancel()
    hold.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    payloads = [payload for _kind, payload in runtime5.write_log]
    assert "enter" not in payloads
    assert "seq-kill" in _unresolved(store5, terminal5.id)
