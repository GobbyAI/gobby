"""WriteCoordinator latch, lock, lease, and sequence tests (plan 2.2)."""

from __future__ import annotations

import asyncio
import os
from typing import Any, Literal, cast

import pytest

from gobby.storage.terminals import (
    UNRESOLVED_WRITE_ACTION_KEY_MAX_BYTES,
    UNRESOLVED_WRITE_MAX_ENTRIES,
    UnresolvedWriteCapacityError,
)
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.runtime import (
    AutomaticWriteQuarantined,
    Delivered,
    IndeterminateWrite,
    TerminalWriteError,
    UnregisteredBackendError,
)
from gobby.terminals.write_coordinator import (
    SequenceDelay,
    StaleTerminalLeaseError,
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


async def _let_tasks_run() -> None:
    """Yield one loop iteration so just-created tasks reach their first await."""
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()
    loop.call_soon(ready.set_result, None)
    await ready


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
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(fake),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )
    return coordinator, fake, store


async def _grant(
    coordinator: WriteCoordinator,
    terminal_id: str,
    attachment_id: str = "att-1",
    *,
    takeover: bool = False,
) -> int:
    await coordinator.lease_registry.attach(terminal_id, attachment_id=attachment_id)
    result = await coordinator.lease_registry.take_control(
        terminal_id,
        attachment_id,
        takeover=takeover,
    )
    assert result.granted
    return result.lease_generation


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
    await _grant(coordinator, terminal.id)

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
    assert isinstance(payloads[1], str)
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
    await _grant(coordinator, terminal.id)
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
        await _let_tasks_run()
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
        await _let_tasks_run()
        assert "seq-mid" in _unresolved(store2, terminal2.id)
        payloads = [payload for _kind, payload in runtime2.write_log]
        assert "enter" not in payloads


@pytest.mark.asyncio
async def test_revalidate_before_persist() -> None:
    hold = asyncio.Event()
    started = asyncio.Event()
    runtime = FakeRuntime(hold=hold)
    runtime.started = started
    coordinator, _fake, store = _coordinator(runtime)
    terminal = next(iter(store.rows.values()))
    await _grant(coordinator, terminal.id)
    await coordinator.lease_registry.attach(terminal.id, attachment_id="att-2")

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
    takeover_task = asyncio.create_task(
        coordinator.lease_registry.take_control(terminal.id, "att-2", takeover=True)
    )
    await _let_tasks_run()

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
    await _let_tasks_run()
    hold.set()
    await holder
    await takeover_task
    await waiter
    assert all(payload != "should-not-land" for _kind, payload in runtime.write_log)
    assert "op" not in _unresolved(store, terminal.id)


@pytest.mark.asyncio
async def test_operator_latch_carries_daemon_epoch() -> None:
    coordinator, runtime, store = _coordinator()
    terminal = next(iter(store.rows.values()))
    generation = await _grant(coordinator, terminal.id)
    runtime.outcome = IndeterminateWrite(detail="reply lost")

    await coordinator.write(
        WriteRequest(
            terminal_id=terminal.id,
            action_key="ws:att-1:1",
            origin="operator",
            kind="text",
            payload="hello",
            attachment_id="att-1",
            expected_lease_generation=generation,
        )
    )

    entry = _unresolved(store, terminal.id)["ws:att-1:1"]
    assert entry["origin"] == "operator"
    assert entry["daemon_epoch"] == coordinator.lease_registry.daemon_epoch
    assert isinstance(entry["at"], str)


@pytest.mark.asyncio
async def test_lease_mutations_linearize_with_dispatch() -> None:
    for mutation in ("takeover", "release", "finalize", "exit"):
        hold = asyncio.Event()
        coordinator, runtime, store = _coordinator(FakeRuntime(hold=hold))
        terminal = next(iter(store.rows.values()))
        generation = await _grant(coordinator, terminal.id)
        if mutation == "takeover":
            await coordinator.lease_registry.attach(terminal.id, attachment_id="att-2")

        write_task = asyncio.create_task(
            coordinator.write(
                WriteRequest(
                    terminal_id=terminal.id,
                    action_key=f"ws:att-1:{mutation}",
                    origin="operator",
                    kind="text",
                    payload=mutation,
                    attachment_id="att-1",
                    expected_lease_generation=generation,
                )
            )
        )
        await runtime.started.wait()
        assert f"ws:att-1:{mutation}" in _unresolved(store, terminal.id)

        mutation_task: asyncio.Task[Any]
        if mutation == "takeover":
            mutation_task = asyncio.create_task(
                coordinator.lease_registry.take_control(
                    terminal.id,
                    "att-2",
                    takeover=True,
                )
            )
        elif mutation == "release":
            mutation_task = asyncio.create_task(coordinator.lease_registry.release_control("att-1"))
        elif mutation == "finalize":
            mutation_task = asyncio.create_task(
                coordinator.lease_registry.finalize("att-1", reason="test")
            )
        else:
            mutation_task = asyncio.create_task(coordinator.clear_on_exit(terminal.id))

        await _let_tasks_run()
        assert not mutation_task.done()
        assert f"ws:att-1:{mutation}" in _unresolved(store, terminal.id)
        hold.set()
        await write_task
        await mutation_task
        assert f"ws:att-1:{mutation}" not in _unresolved(store, terminal.id)

    registry = TerminalLeaseRegistry(daemon_epoch="test-epoch")
    attachment = await registry.attach("term-reuse", attachment_id="att-old")
    async with registry.lock("term-reuse"):
        cell = registry._lock_cells["term-reuse"]
        finalize_task = asyncio.create_task(
            registry.finalize(attachment.attachment_id, reason="test")
        )
        await _let_tasks_run()
        reattach_task = asyncio.create_task(registry.attach("term-reuse", attachment_id="att-new"))
        await _let_tasks_run()
        assert registry._lock_cells["term-reuse"] is cell
    await finalize_task
    reattached = await reattach_task
    assert reattached.attachment_id == "att-new"
    assert "term-reuse" not in registry._lock_cells

    entered = asyncio.Event()

    async def queued_waiter() -> None:
        async with registry.lock("term-cancel"):
            entered.set()

    async with registry.lock("term-cancel"):
        owner_cell = registry._lock_cells["term-cancel"]
        waiter = asyncio.create_task(queued_waiter())
        await _let_tasks_run()
        assert registry._lock_cells["term-cancel"] is owner_cell
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not entered.is_set()
        assert registry._lock_cells["term-cancel"] is owner_cell
        assert registry.lock_held("term-cancel")
    assert "term-cancel" not in registry._lock_cells

    await coordinator.write(
        WriteRequest(
            terminal_id=terminal.id,
            action_key="attn",
            origin="attention",
            kind="text",
            payload="attention-ok",
        )
    )
    assert any(
        isinstance(payload, str) and payload.startswith("attention-ok")
        for _kind, payload in runtime.write_log
    )


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
        store_big.persist_unresolved_write(
            terminal_big.id,
            "big",
            huge_origin,
            daemon_epoch="test-epoch",
        )
    assert runtime_big.write_log == []

    existing = filled.copy()
    existing_key = next(iter(existing))
    coordinator_existing, runtime_existing, store_existing = _coordinator(unresolved=existing)
    terminal_existing = next(iter(store_existing.rows.values()))
    await _grant(coordinator_existing, terminal_existing.id)
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
            daemon_epoch: str,
            at: Any = None,
            payload_fingerprint: str | None = None,
        ) -> Any:
            super().persist_unresolved_write(
                terminal_id,
                action_key,
                origin,
                daemon_epoch=daemon_epoch,
                at=at,
                payload_fingerprint=payload_fingerprint,
            )
            raise RuntimeError("hard-kill")

    terminal = make_memory_terminal()
    store = KillAfterPersist(terminal)
    runtime = FakeRuntime()
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(runtime),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )
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
    coordinator2 = WriteCoordinator(
        cast(UnresolvedWriteStore, store2),
        runtime_registry(runtime2),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )
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
    coordinator3 = WriteCoordinator(
        cast(UnresolvedWriteStore, store3),
        runtime_registry(runtime3),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )
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
    coordinator4 = WriteCoordinator(
        cast(UnresolvedWriteStore, store4),
        runtime_registry(runtime4),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )
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
    coordinator5 = WriteCoordinator(
        cast(UnresolvedWriteStore, store5),
        runtime_registry(runtime5),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )
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


def _wake_steps(terminal_id: str) -> list[WriteRequest]:
    return [
        WriteRequest(
            terminal_id=terminal_id,
            action_key="wake",
            origin="automatic",
            kind="text",
            payload="hello",
        ),
        WriteRequest(
            terminal_id=terminal_id,
            action_key="wake",
            origin="automatic",
            kind="key",
            payload="enter",
        ),
    ]


@pytest.mark.asyncio
async def test_sequence_failure_before_any_dispatch_clears_latch() -> None:
    """Nothing reached the terminal, so nothing is left to resolve."""
    coordinator, runtime, store = _coordinator(
        FakeRuntime(raise_on_write=TerminalWriteError(stage="none"), raise_on_write_after=0)
    )
    terminal = next(iter(store.rows.values()))

    with pytest.raises(TerminalWriteError):
        await coordinator.run_sequence(
            terminal.id, action_key="wake", origin="automatic", steps=_wake_steps(terminal.id)
        )

    assert _unresolved(store, terminal.id) == {}
    assert runtime.write_log == []


@pytest.mark.asyncio
async def test_sequence_failure_after_a_dispatched_step_keeps_latch() -> None:
    """A step that landed may have changed the terminal; the action stays open."""
    coordinator, runtime, store = _coordinator(
        FakeRuntime(raise_on_write=TerminalWriteError(stage="none"), raise_on_write_after=1)
    )
    terminal = next(iter(store.rows.values()))

    with pytest.raises(TerminalWriteError):
        await coordinator.run_sequence(
            terminal.id, action_key="wake", origin="automatic", steps=_wake_steps(terminal.id)
        )

    assert "wake" in _unresolved(store, terminal.id)
    assert runtime.write_log == [("text", "hello")]


@pytest.mark.asyncio
async def test_dispatch_resolves_the_runtime_per_terminal_backend() -> None:
    """One coordinator, two backends: each write reaches only its own runtime.

    The coordinator used to bind a single runtime at construction, so every
    write to a native-backend terminal was injected through the tmux runtime.
    """
    tmux_terminal = make_memory_terminal(backend="tmux")
    native_terminal = make_memory_terminal(backend="native")
    store = MemoryTerminalStore(tmux_terminal)
    store.rows[native_terminal.id] = native_terminal
    tmux_runtime = FakeRuntime(backend="tmux")
    native_runtime = FakeRuntime(backend="native")
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(tmux_runtime, native_runtime),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )

    for terminal, payload in ((tmux_terminal, "to-tmux"), (native_terminal, "to-native")):
        await coordinator.write(
            WriteRequest(
                terminal_id=terminal.id,
                action_key=f"w:{terminal.id}",
                origin="automatic",
                kind="text",
                payload=payload,
            )
        )

    assert tmux_runtime.write_log == [("text", "to-tmux")]
    assert native_runtime.write_log == [("text", "to-native")]


@pytest.mark.asyncio
async def test_unregistered_backend_is_a_stage_none_write_error() -> None:
    """No runtime owns the backend, so no bytes reached the terminal."""
    terminal = make_memory_terminal(backend="native")
    store = MemoryTerminalStore(terminal)
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(FakeRuntime(backend="tmux")),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )

    with pytest.raises(TerminalWriteError) as excinfo:
        await coordinator.write(
            WriteRequest(
                terminal_id=terminal.id,
                action_key="unroutable",
                origin="automatic",
                kind="text",
                payload="x",
            )
        )

    assert excinfo.value.stage == "none"
    assert isinstance(excinfo.value.__cause__, UnregisteredBackendError)
    assert excinfo.value.__cause__.backend == "native"
    # A provably-empty write must not leave the action latched, or every later
    # automatic write to this terminal is suppressed as a duplicate.
    assert _unresolved(store, terminal.id) == {}


def _drain_step(terminal_id: str) -> WriteRequest:
    return WriteRequest(
        terminal_id=terminal_id,
        action_key="drain",
        origin="automatic",
        kind="key",
        payload="ctrl_u",
    )


@pytest.mark.asyncio
async def test_unlatched_sequence_leaves_no_entry_for_a_lost_reply() -> None:
    """An idempotent action opts out of the latch, so a lost reply cannot suppress its retry."""
    coordinator, runtime, store = _coordinator(
        FakeRuntime(outcomes=[IndeterminateWrite(detail="lost")])
    )
    terminal = next(iter(store.rows.values()))

    lost = await coordinator.run_sequence(
        terminal.id,
        action_key="drain",
        origin="automatic",
        steps=[_drain_step(terminal.id)],
        latch=False,
    )

    assert isinstance(lost, IndeterminateWrite)
    assert _unresolved(store, terminal.id) == {}

    retried = await coordinator.run_sequence(
        terminal.id,
        action_key="drain",
        origin="automatic",
        steps=[_drain_step(terminal.id)],
        latch=False,
    )

    assert isinstance(retried, Delivered)
    assert runtime.write_log == [("key", "ctrl_u"), ("key", "ctrl_u")]


@pytest.mark.asyncio
async def test_unlatched_sequence_still_honours_quarantine() -> None:
    """Opting out of the latch does not opt out of another action's quarantine."""
    coordinator, runtime, store = _coordinator()
    terminal = next(iter(store.rows.values()))
    store.set_automatic_write_quarantine(terminal.id, "handoff:compact")

    outcome = await coordinator.run_sequence(
        terminal.id,
        action_key="drain",
        origin="automatic",
        steps=[_drain_step(terminal.id)],
        latch=False,
    )

    assert isinstance(outcome, AutomaticWriteQuarantined)
    assert runtime.write_log == []


@pytest.mark.asyncio
async def test_input_with_a_client_fd_writes_the_attach_client_pty() -> None:
    """A request carrying the attach client's PTY bypasses the pane runtime."""
    coordinator, runtime, store = _coordinator()
    terminal_id = next(iter(store.rows))
    read_fd, write_fd = os.pipe()
    try:
        outcome = await coordinator.write(
            WriteRequest(
                terminal_id=terminal_id,
                action_key="ws:att:1",
                origin="automatic",
                kind="input",
                payload="\x1b[<64;13;12M",
                client_fd=write_fd,
            )
        )

        assert isinstance(outcome, Delivered)
        assert os.read(read_fd, 64) == b"\x1b[<64;13;12M"
        assert runtime.write_log == []
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.asyncio
async def test_client_fd_write_failure_before_any_byte_is_stage_none() -> None:
    """A dead attach client refuses the write instead of reporting bytes it never sent."""
    coordinator, runtime, store = _coordinator()
    terminal_id = next(iter(store.rows))
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)

    with pytest.raises(TerminalWriteError) as excinfo:
        await coordinator.write(
            WriteRequest(
                terminal_id=terminal_id,
                action_key="ws:att:2",
                origin="automatic",
                kind="input",
                payload="x",
                client_fd=write_fd,
            )
        )

    assert excinfo.value.stage == "none"
    assert isinstance(excinfo.value.__cause__, OSError)
    assert runtime.write_log == []
